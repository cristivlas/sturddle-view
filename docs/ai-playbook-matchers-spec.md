# AI Playbook Matchers - Spec (tentative)

Status: draft for review. Companion to `docs/ai-playbook-spec.md`, which
stays the reference for today's tags, fragments and combination rules.

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
- `combo`: `when` is `margin` plus exactly one of `phase` / `structure`
  (the only pairs the renderer looks up: margin+phase, then
  margin+structure); replaces both axes' fragments.
  The axes it consumes are its `when` keys. Phase combos and structure
  combos behave differently today (a phase combo keeps the specific
  tags, a structure combo keeps the phase slot); that branching stays in
  code, keyed on whether `phase` is among the consumed axes.
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

## Later stages (not specified yet)

- **Declarative pawn-layout matcher.** Patterns as pawn placements with
  wildcards, color-normalized and file-mirrored, matched by similarity
  score. Lets structure entries (Carlsbad, Maroczy, hanging pawns, ...)
  carry their own pattern in the file instead of a coded predicate.
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
