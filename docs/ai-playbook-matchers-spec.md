# AI Playbook Matchers - Spec (tentative)

Status: stages 1 and 2 implemented on `feat/0.7.7`.
Companion to `docs/ai-playbook-spec.md`, which stays the reference for
today's tags, fragments and combination rules.

## Goal

Grow the playbook past a handful of hard-coded tags without growing
`play/playbook.py` and `llm/playbook.py` into one big switch:

- Pattern recognition behind one abstraction (the matcher protocol), so
  new recognizers -- hand-coded, declarative, model-based -- plug in
  without touching the renderer or the gate.
- Plan text on disk (playbook files), keyed by tags, with provenance,
  so content can be shipped, overridden, and later researched.

Extensible by design, not at runtime: matchers are code registered in a
fixed order; files are data.

## Stage 1 scope (this branch)

A refactor with no behavior change:

1. Wrap today's specific-tag predicates as matchers.
2. Move today's fragment and note text out of `llm/playbook.py` into
   shipped playbook files keyed by the same tags.

### Acceptance

- **Behavior:** the rendered plan line is identical before and after.
  Checked once with a throwaway script (not committed: no byte-stable
  tests, project rule) that dumps `classify` + `render_playbook` output
  on `feat/0.7.6` and diffs it on this branch: the test files' FENs and
  seeded random games under 10 evals x both colors x both modes, and
  every `Situation` field combination.
- **Tests:** `test_playbook.py` changes mechanically. It imports the
  private fragment dicts today (`_MARGIN_FRAGMENTS`, `_COMBO_FRAGMENTS`,
  ...); those lookups become catalog lookups by tag (notes by id). Assertions
  keep their meaning (which fragment, in which order); no test is
  dropped.

Out of scope here: aggression / speculative sacrifices (own spec, see
Later stages), any new tag, any new content source.

## Vocabulary

- **Base tags** -- `margin` and `phase`: computed by `classify()` before
  any matcher runs; every matcher may read them.
- **Matcher** -- recognizes zero or more patterns in a position.
- **Match** -- one recognized pattern: axis, tag.
- **Entry** -- one piece of plan text plus the tags that select it.
- **Catalog** -- all loaded entries, merged and validated.
- **Trust** -- how far an entry's content has been vetted.

## Matcher protocol

In-process Python `Protocol`; no MCP, no IPC.

```python
class Matcher(Protocol):
    name: str                                  # stable id
    tags: Mapping[str, frozenset[str]]         # axis -> tags it can emit
    def match(self, ctx: MatchContext) -> Sequence[Match]: ...

@dataclass(frozen=True, slots=True)
class MatchContext:
    board: chess.Board
    our_color: chess.Color
    margin: str                    # base tag
    margin_source: str             # eval | material
    phase: str                     # base tag

@dataclass(frozen=True, slots=True)
class Match:
    axis: str                      # structure, imbalance, bishops, ...
    tag: str
```

- Matchers are pure functions of the context: no engine search, no I/O
  on the hot path (same rule as today's classifier).
- `tags` is the declared vocabulary: the coverage test and user-file
  validation read it. A match outside it is a bug (asserted in tests).
- One tag per axis, as today; each axis has exactly one matcher.
- No match score or evidence in stage 1: today's predicates are boolean
  and nothing would read either. Both come as defaulted (non-breaking)
  fields with their first consumer: a score with the first similarity
  matcher (with how scores compare across matchers), evidence (squares,
  pieces, a game reference) with the first renderer that shows it.
- Phase gating ("not in the endgame") stays inside each matcher, read
  from `ctx.phase`.

### Registry

- An ordered tuple of matcher instances in `play/matchers.py`, which
  also holds the predicates and their thresholds.
- Tag vocabulary (axes, tags) in `play/playbook_tags.py`, shared by the
  classifier, the matchers, the catalog and the renderer (breaks the
  classifier <-> registry import cycle).
- Stage 1 matchers: structure, imbalance, bishops, pawn, castling.
- Margin and phase are not matchers: they are base tags, computed first
  so matchers can read them (no circularity).
- Repetition stays out of the protocol: it reads the move stack and
  feeds the gate and a code-selected note, not tag lookup.
- **Registry order is render order** for the specific tags (today:
  imbalance, bishops, pawn, castling). Structure renders last, as today;
  its position is a combination rule (code), not registry order.

### Situation

- `classify()` keeps its signature and still returns `Situation`: it
  computes the base tags, builds the context, runs the registry, and
  folds matches into the dataclass fields.
- The gate (`tools_engine`, repetition veto) and `HumanVsEngine` keep
  reading `Situation` unchanged.
- `Situation` gains `matches: tuple[Match, ...]` (registry order) for
  the renderer and future consumers.

## Playbook files

### Format

TOML (stdlib `tomllib`, Python 3.12). Entries are short by design (plan
line < 80 words); long source material belongs in citations, not
entries, so Markdown's prose comfort buys nothing. One file per kind:
`fragments.toml`, `combos.toml`, `notes.toml`.

```toml
[meta]
source = "shipped"                 # shipped | user | research
trust = "trusted"                  # trusted | unverified
# research files also: urls, retrieved (date), reviewed_by

[[entry]]
id = "imbalance.iqp_ours"
when = { imbalance = "iqp_ours" }
text = "isolated d-pawn: control its stop square, play actively now, avoid endings where it is a target."

[[entry]]
id = "combo.losing_closed"
kind = "combo"
when = { margin = "losing", structure = "closed" }
text = "behind in a closed position: keep it closed, ..."

[[entry]]
id = "note.repeat_behind"
kind = "note"
text = "A repetition is available ($moves): repeating is the drawing try; take it unless a move clearly improves."
```

### Kinds

- `fragment` (default): `when` has exactly one key.
- `combo`: `when` is `margin` plus exactly one of `bishops` / `phase` /
  `structure` (the only pairs the renderer looks up, in that order:
  margin+bishops, margin+phase, then margin+structure); replaces both
  axes' fragments. The axes it consumes are its `when` keys. Phase
  combos and structure combos behave differently (a phase combo keeps
  the specific tags, a structure combo keeps the phase slot); a bishops
  combo renders like a phase combo, minus the bishops and structure
  fragments. That
  branching stays in code.
- `note`: no `when`; selected by id from code (crushing / lost notes,
  repetition notes). Notes are the only kind with template fields.

### Margin normalization

- `when` is plain equality, no sets, no negation, in stage 1.
- The renderer normalizes the margin before any fragment or combo lookup:
  `lost` -> `losing`, `crushing` -> `winning` (today's `_BASE_MARGIN`
  plus the `_MARGIN_FRAGMENTS` aliases). Files key on the base margins
  only.
- The extreme margins still select their own notes, by id from code.

### What moves to data, what stays in code

- Data: fragments, combos, notes.
- Code: the combination rules (phase fallback, endgame ordering,
  fragment cap, note slot, mode gating of the lost note, margin
  normalization) and the standing phrase (margin words, "by material",
  the verb). The standing phrase is grammar assembled per mode, not
  tag-selected text.

### Templates

- `string.Template` (`$moves`), never `str.format` -- user-file text is
  untrusted and `str.format` reaches attributes (`{moves.__class__}`).
- Each note id declares its allowed identifiers in code
  (`note.repeat_*`: `moves`; the others: none). A file using any other
  identifier fails validation (`Template.get_identifiers`).

### Location and layering

- Catalog loader `play/playbook_catalog.py`, shipped data
  `play/playbook_data/*.toml` (package data in `pyproject.toml`; the
  desktop exe bundles the dir via `--add-data` in
  `scripts/build_exe.py`). Next to the matchers, so later declarative
  patterns (match data in files) live with the code that reads them.
- `llm/playbook.py` keeps rendering, reading text from the catalog (it
  already imports `play.playbook`).
- User files: `app_config_dir() / "playbook" / *.toml`; tests point it
  at a temp dir (conftest); path
  overridable via `SV_AI_PLAYBOOK_DIR`.
- `load_catalog(shipped_dir, user_dir) -> Catalog`: pure, immutable
  result, no module state. Tests call it on temp dirs.
- `render_playbook(situation, mode, side_to_move, catalog=None)`: an
  explicit catalog wins; `None` reads `default_catalog()`, loaded on
  first use from the real dirs and cached; the app lifespan warms it,
  so a bad shipped file fails boot, not a live plan call. No hot reload
  in stage 1.

### Overrides

- Load order shipped, then user.
- A user entry with a shipped `id` replaces the text only. Its `when`
  and `kind`, if given, must equal the shipped ones; otherwise the entry
  is rejected (no re-keying a shipped id).
- A user entry with a new `id` adds; a fragment or combo must not
  collide with an existing `(kind, when)`.
- Provenance is per entry after merge: an override carries its own
  file's `[meta]` (source, trust), not the shipped file's.
- A user-dir file's `source` is `user` (default) or `research`; one
  claiming `shipped` is rejected whole.

### Validation at load

Invariants over the merged catalog:

- `id` unique per source.
- `(kind, when)` unique for fragments and combos: two entries for
  `imbalance = "iqp_ours"` are an error, not a choice. Notes have no
  `when` and are unique by `id`.
- Every `when` key is a known axis and every value a declared tag (base
  tags or a matcher's `tags`); for margins, a base margin.
- `when` arity matches the kind; `text` non-empty; template identifiers
  allowed for the id.
- Coverage: every declared tag has a fragment entry, except tags with
  no fragment by design (`margin = "even"`; `lost` / `crushing` reach
  theirs through normalization). Every note id the code selects exists.

Failure handling: shipped files fail the test suite; user files are
logged and the bad entry skipped (never crash the server).

`[meta]` fields are validated (known `source` / `trust` values) and
recorded per entry, but nothing reads `trust` in stage 1: the render
gate for unverified entries comes with the first research file.

## Stage 2: pawn formations

The first data-defined matcher: named pawn formations (Maroczy bind,
hanging pawns, Hedgehog, Stonewall, ...) declared in playbook files,
with their plan text next to them. Adding a formation is a data change.

### Axis and tags

- New axis `formation`. Tags are `<pattern id>_ours` / `<pattern
  id>_theirs`, our side's point of view, as for `imbalance`.
- One tag per axis, as today. Several patterns matching at once: the
  most specific wins (most constraints), then load order (sorted
  filename, shipped before user, so a user pattern loses a tie to a
  shipped one).
- Specificity resolves refinements on purpose. A Hedgehog with no White
  d-pawn is also a Maroczy from White's side; Hedgehog (7 constraints)
  beats Maroczy (5), so White sees `hedgehog_theirs`, never
  `maroczy_ours`. Intended: the Hedgehog is the more precise
  description, and `hedgehog_theirs` text carries the bind plan (keep
  the clamp, stop ...b5 / ...d5).
- Render order: right after `imbalance` (registry order), so it competes
  with the other specific tags under the fragment cap.
- Shipped patterns do not restate a coded tag. IQP and the Carlsbad
  minority attack stay coded on `imbalance`: IQP's "not passed" is
  relational, and a relational grammar for one tag is not worth it.

### Pattern format

A new `[[pattern]]` array in playbook files:

```toml
[[pattern]]
id = "maroczy"
ours = { c = "4", d = "-", e = "4" }
theirs = { c = "-", d = "3" }
examples = ["r1bqkb1r/pp2pppp/2np1n2/8/2PNP3/8/PP3PPP/RNBQKB1R w KQkq - 0 1"]
counterexamples = ["r1bqkb1r/pp2pppp/2np1n2/8/3NP3/8/PPP2PPP/RNBQKB1R w KQkq - 0 1"]

[[entry]]
id = "formation.maroczy_ours"
when = { formation = "maroczy_ours" }
text = "..."
```

- `ours` / `theirs`: one constraint per file, files left out are free.
  Values, ranks relative to that side (2 = its home rank):
  - `"-"`: no pawn of that side on the file;
  - `"*"`: at least one pawn on the file;
  - `"4"`: a pawn on that rank; `"3-4"`: a pawn on one of those ranks.
  File keys `a`-`h` only; ranks 2-7, a range low <= high. Anything else
  invalidates the pattern.
  A file constraint holds if any of the side's pawns on it fits
  (doubled pawns are not expressible; not needed yet).
- Color-normalized: the matcher reads the board from each side in turn;
  `ours` binds to the side being tested. Holds for us -> `_ours`, for
  the opponent -> `_theirs`; both at once -> no tag (ambiguous).
- `mirror` (default false): also try the a<->h file mirror. Off by
  default: most formations mean something on their wing.
- `phases` (default: every phase but `endgame`): where the pattern may
  fire, matching the "structure is middlegame talk" rule.
- Exact match. Tolerance lives in the pattern (rank ranges, free files),
  not in a score: a near miss stays a miss, in line with "fewer fragments
  beats wrong ones". The similarity score stays deferred.

### Self-checking patterns

- `examples` (at least one) and `counterexamples` (at least one) are
  FENs; the side to move is `ours` (so a Hedgehog example has Black to
  move). At load, each example must match `_ours` for the side to move
  and `_theirs` for the other side, and each counterexample must match
  neither. A failing pattern is invalid (shipped: error; user:
  skipped and logged).
- At least three constraints across both sides, so a pattern cannot
  fire on half the positions in the game.
- The per-pattern check cannot see overlaps, so each example is also run
  through the whole formation matcher (after the tie-break) and must
  yield its own pattern's tag. Phase gating applies, so an example must
  sit in one of the pattern's `phases`. Shipped patterns are checked against
  the shipped set (error on failure). Each user pattern is added in load
  order only if every example, shipped and user, still yields its tag;
  otherwise it is skipped and logged (a user pattern cannot steal a
  shipped example).

### Catalog, registry, classify

- Load runs in passes over all files (shipped, then user): (1)
  patterns, so a pattern in one file can be referenced from another;
  (2) entries, validated against a vocabulary that includes the
  formation tags; (3) coverage: a user pattern missing its `_ours` or
  `_theirs` fragment is dropped, with any fragment that names it.
  Shipped coverage gaps are errors, as in stage 1.
- The catalog builds one `FormationMatcher` (`play/formations.py`) from
  the surviving patterns and exposes `Catalog.matchers`: the static
  `REGISTRY` with the formation matcher spliced in after `imbalance`
  (`compose`).
- Pattern ids are lowercase identifiers; example FENs must be valid
  positions.
- Overrides: a user file may add patterns; a shipped pattern id cannot
  be redefined (same rule as re-keying an entry). Its fragments'
  text is overridable as usual.
- `classify(..., matchers=REGISTRY)`: the classifier never sees the
  catalog and stays a pure function of its arguments (no file I/O in
  `play/playbook.py`). `HumanVsEngine.situation` passes
  `default_catalog().matchers`. The `Situation` gains a `formation`
  field.
- Render order comes from the same composed registry: the renderer
  derives its specific axes from `catalog.matchers`
  (`Catalog.specific_axes`); the static `SPECIFIC_AXES` is gone (a
  catalog-only matcher would never render).

### Shipped content

Nine formations not covered by a coded tag, in
`play/playbook_data/formations.toml`. Squares are relative to the side
(Black's d6 is d3). The last five came from mining 5,000 engine games
for common central structures; on that sample the nine together fire
(held for 3+ moves, opening / middlegame) in about 29% of games, the
first four alone in about 5%:

| id | ours | theirs | plan gist (ours) |
|----|------|--------|------------------|
| `maroczy` | c4, e4, no d-pawn | no c-pawn, d3 | keep the bind, stop the ...b5 / ...d5 breaks, squeeze |
| `hanging` | c4 + d4, no b- / e-pawn | no c- or d-pawn | keep them mobile, push d5 at the right time |
| `hedgehog` | a3 b3 d3 e3, no c-pawn | c4, e4 | wait, prepare the ...b5 or ...d5 break |
| `french_chain` | d4, e5 | d4, e3 | defend the chain's base, kingside space |
| `stonewall` | c3 d4 e3 f4 | free | e5 outpost, kingside attack; mind the e4 hole |
| `kid_center` | d5, e4 | d3, e4 | c-pawn break on the queenside; meet the f-pawn storm |
| `benoni` | no c-pawn, d5 | c4, d3, no e-pawn | keep the wedge, prepare e-pawn break |
| `caro_kann` | c2-3, d4, no e-pawn | c3, no d-pawn, e2-3 | space, central outpost before the c-pawn break |
| `open_sicilian` | c2-3, no d-pawn, e4 | no c-pawn, d3 | development lead, half-open d-file, attack |

Rows are in load order. `french_chain` loads before `stonewall`: both
have four constraints and can hold at once (a Stonewall against an
enemy pawn on e5), and there the chain is the right plan, since the
Stonewall hole holds the enemy pawn. The grammar cannot say "no enemy
pawn on the hole", so load order decides.

### Stage 2 testing

- Pattern grammar: each value form, mirror, phases, color flip.
- Self-checks run over shipped patterns (examples, counterexamples,
  both sides); a broken example fails load. Whole-matcher check: every
  shipped example yields its own tag.
- Grammar validation: bad file key, rank outside 2-7, inverted range.
- User patterns: cross-file reference works; a pattern missing a
  fragment is dropped with its other fragment; a pattern that steals a
  shipped example is skipped; ties go to shipped.
- Specificity tie-break; both-orientation ambiguity yields no tag.
- Rendering: formation fragment in registry order and under the cap.
- Baseline: positions without a formation render exactly as stage 1.

### Stage 2 open questions

- Symmetric formations (both sides the same): ambiguous today; worth a
  neutral tag?
- Do any formations need a material condition (queens on, minor-piece
  balance)? None in the first four.

## Later stages (not specified yet)

- **Similarity matching.** A score for near-miss formations, with how
  scores compare across matchers (stage 2 is exact match).
- **Game-reference matcher.** Matches the position against a curated
  game index; evidence is the reference ("Kasparov, game 6 of ...").
  Hard rule: references come from the index only, never from model
  memory. Human games are fine here: illustration, not prescription.
- **Model-based matcher.** A model labels patterns; likely offline with
  results cached per position, since live labeling is slow and noisy.
- **Researched content.** Online research written to `source =
  "research"`, `trust = "unverified"` files with citations; concrete
  lines engine-checked, prose promoted to trusted only by human review.
- **Game sources.** Machine games (TCEC, CCRL), correspondence, curated
  human classics -- to be chosen by experiment. Plans can be extracted
  by running the matchers over a game and recording what followed
  (pawn breaks, piece routes, trades).
- **Aggression / speculative sacrifices.** Separate spec: the model
  proposes a sacrifice by pattern, the engine verifies that one move with
  a focused deep search (`searchmoves`), accepted within a wider margin.
  Addresses sacs a shallow full search misses.

## Testing

- `test_playbook.py`: mechanical port from private dicts to catalog
  lookups (see Acceptance).
- Matchers: each returns the same tag as the old predicate over the
  existing FEN tables; every emitted match is in its declared `tags`.
- Catalog: the validation invariants above, run over the shipped files.
- Overrides: replace by id, re-key rejected, new id added, `(kind,
  when)` collision rejected, bad template identifier rejected; each bad
  user entry skipped and logged.
- No byte-stable text assertions (project rule); assert the entry a tag
  selects.

## Voice

No per-mode variants: entry text stays voice-neutral (imperative, no
pronouns, "enemy" for the other side), as today. Mode-dependent wording
is the standing phrase, built in code.
