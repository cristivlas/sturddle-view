# AI Playbook v2 -- Implementation plan

Extends the shipped playbook (`docs/ai-playbook-spec.md`) with finer
structural tags and a tone fix. No new round-trips: still one `Plan:`
line, injected inline, same mechanism as v1. Classifier changes live in
`play/playbook.py`, fragments and rendering in `llm/playbook.py`; the
`classify` / `render_playbook` signatures, the wire-in
(`_ai_kick._playbook_for`) and the gate are untouched.

## 1. Tone fix

The PHASE_OPENING fragment (`llm/playbook.py`) reads "development, the
center and king safety before adventures". Replace "adventures" with
"premature attacks". The other fragments were swept and are on register;
nothing else changes. Fragment text is a steer, not a quote, but register
must match the catalog: imperative, no first or second person, no
informal idiom.

## 2. Tag vocabulary and `Situation` fields

Four new fields on `Situation`, all `str | None`, all defaulting to
`None` (keeps `Situation(**kwargs)` call sites and the `_situation()`
test helper working unchanged), declared after `repeats`:

| Field | Values (module constants in `play/playbook.py`) |
|-------|--------------------------------------------------|
| `imbalance` | `iqp_ours`, `iqp_theirs`, `minority_ours`, `minority_theirs` |
| `bishops`   | `opposite_queens`, `opposite` |
| `pawn`      | `passed_outside_ours`, `passed_outside_theirs` |
| `castling`  | `opposite` |

Directional tags (`_ours` / `_theirs`) take our side's point of view,
exactly like `margin`: coach = the human, commentator = the side to move
(`_playbook_for` already passes that `our_color` into `classify`).
`bishops` and `castling` are symmetric facts, not directional.

`imbalance` here means pawn-skeleton shape. The v1 spec's deferred
material `imbalance` (exchange up, minor vs pawns) is renamed to
`material` in the spec's deferred list so the names do not collide.

## 3. Detectors (deterministic, python-chess, `play/playbook.py`)

Each is a cheap board predicate in the style of `_locked_pairs` /
`_file_counts`, computed inside `classify`. Gating:

| Tag | Fires in |
|-----|----------|
| `imbalance` | `phase != endgame` (structure advice is middlegame talk) |
| `castling`  | `phase != endgame` and both queens on the board |
| `bishops`   | every phase |
| `pawn`      | every phase (the endgame gate exists because few pawns make every file open, which says nothing about a passer; the outside passer is foremost an endgame asset) |

New shared helper: `_passed_pawns(board, color) -> chess.SquareSet`:
pawns of `color` with no enemy pawn on the same or an adjacent file on
any rank ahead of them. Used by the IQP and outside-passer predicates.

### `imbalance=iqp_*`

Side `c` has the IQP when all of:

- exactly one pawn of `c` on the d-file;
- no pawn of `c` on the c- or e-file;
- no pawn of `not c` on the d-file;
- the d-pawn is not passed (a passed d-pawn is an asset, not an IQP).

At most one side can satisfy this (each needs the other's d-file empty).
Tag `iqp_ours` when `c == our_color`, else `iqp_theirs`.

### `imbalance=minority_*`

Carlsbad shape. Side `m` is the minority side when all of:

- `m` has exactly one pawn on the a-file, one on the b-file, none on
  the c-file;
- `not m` has exactly one pawn on each of the a-, b- and c-files;
- the d-file holds a locked pair: the white d-pawn has the black d-pawn
  directly in front of it (same test as `_locked_pairs`, d-file only);
- both sides have the same number of pawns.

Tag `minority_ours` when `m == our_color`, else `minority_theirs`.

IQP and minority are mutually exclusive by construction (IQP needs the
other side's d-file empty; minority needs both d-pawns). `classify`
checks IQP first anyway; the order is documented, not load-bearing.

### `bishops=opposite*`

Fires when exactly one bishop per side, on opposite-colored squares
(`chess.BB_LIGHT_SQUARES` / `chess.BB_DARK_SQUARES`), and no knights on
the board at all (a knight can trade itself for a bishop, which voids
both the attacking and the drawing logic). Value:

- `opposite_queens` when both sides still have a queen (any phase);
- `opposite` otherwise.

### `pawn=passed_outside_*`

`outside(c)` holds when `_passed_pawns(board, c)` has a pawn on the a-,
b-, g- or h-file. Tag only when exactly one side satisfies it (both is a
race, no tag): `passed_outside_ours` / `passed_outside_theirs`.

### `castling=opposite`

King squares only, no move history: an artificially castled king
(Kf1-g1) plays the same race and it works on a bare FEN. No pawn-shield
check. Fires when either:

- white king on files a-c and rank 1-2, black king on files g-h and
  rank 7-8; or
- white king on files g-h and rank 1-2, black king on files a-c and
  rank 7-8;

plus the gate in the table above (not endgame, both queens on).

## 4. Fragment catalog

Gists; final text ~15 words each (v1 sizing), named string constants in
`llm/playbook.py`, one dict per axis keyed on the tag value. Voice as in
v1: imperative plan language, "enemy" marks the other side, no pronouns,
so the same text reads under both the coach and the commentator lead.

- `iqp_ours`: the isolated d-pawn buys activity now; control the square
  in front of it, use the pieces before it becomes a weakness, avoid an
  ending where it is only a target.
- `iqp_theirs`: blockade the enemy isolated d-pawn with a knight on the
  stop square, trade the active minor pieces, steer toward an ending
  where the pawn is fixed and weak.
- `minority_ours`: advance the a- and b-pawns and trade one off to leave
  a backward or isolated pawn, then target it.
- `minority_theirs`: counter in the center or on the kingside; the
  queenside pawn the enemy attack weakens will need a defender.
- `opposite_queens`: opposite-colored bishops with queens on: the
  attacker keeps queens on and plays on the squares its bishop controls.
- `opposite`: opposite-colored bishops: mass trades drift toward a draw;
  the side ahead needs a second front or an outside passer to press.
- `passed_outside_ours`: push and support the outside passed pawn; it
  ties enemy pieces down.
- `passed_outside_theirs`: blockade the enemy outside passer with a minor
  piece, without leaving a piece stuck doing only that job.
- `castling=opposite`: opposite-side castling: pawn-storm the wing with
  the enemy king, keep the shield in front of the own king; every tempo
  counts.

## 5. Rendering and precedence (`_fragments`)

Specific tags render in this fixed order and a cap drops from the tail:
`imbalance`, then `bishops`, then `pawn`, then `castling`. Call that
list `specific`.

Outside the endgame the phase fragment is the generic fallback: any
specific tag replaces it. Kept, it would starve the specific tags and
sometimes contradict them (`late` "trade toward a favorable ending" vs
opposite bishops "mass trades drift toward a draw"; `opening` "king
safety before premature attacks" vs the opposite-castling race). In the
endgame the phase fragment leads as in v1 and the specific tags follow.

Combo lookup is unchanged from v1 (phase pair first, then structure
pair). The axis list is then:

| Case | Axis fragments, in order |
|------|--------------------------|
| endgame, combo | combo, specific... |
| endgame, no combo | phase, margin (if any), specific... |
| combo on the phase axis | combo, specific..., structure (if any) |
| combo on the structure axis | combo, then specific... if any else phase |
| no combo | margin (if any), then specific... if any else phase, then structure (if any) |

`margin=even` has no margin fragment (v1). `structure` is always `None`
in the endgame (v1). When no specific tag fires every row reduces to
v1's output.

### Note slot (fixes a latent v1 defect)

v1 appends `_CRUSHING_NOTE` / `_LOST_NOTE` to the list and then slices
the whole list to `MAX_FRAGMENTS`. v1 never overflows: with a note
pending the axis list is at most 2 (combo + one axis, or margin +
phase), so the note is always the third item. Any new fragment breaks
that and the slice would drop the resign / convert advice. Fix: notes
have priority over axis fragments:

```
notes = [crushing or lost note]      # same conditions as v1, at most one
return axis[:MAX_FRAGMENTS - len(notes)] + notes
```

`MAX_FRAGMENTS` stays 3. The repetition note keeps riding outside the
cap (v1, unchanged).

### Combos

No new combo-override rows in this extension: ship the axes standalone
and see which pairs turn out contradictory live before hand-writing
overrides (that is how v1's combo table got justified). Pairs to watch:
`winning` + `endgame` ("trade pieces not pawns") vs `bishops=opposite`;
`worse` + `opening` ("castle and consolidate") vs `castling=opposite`.

## 6. Non-goals / what stays on-demand-via-tool (NOT inlined)

- Named endgame patterns requiring a pattern library (Lucena, Philidor,
  Vancura) -- still deferred, per v1 spec. These need position matching
  beyond cheap predicates; if ever added, they are a candidate for an
  actual tool call (`related_endgames`-style), not a `Plan:` fragment,
  because they are rare enough that paying inline-injection cost on
  every turn is not justified.
- Opening-specific theory beyond the existing `related_openings` tool
  and `in_opening` steer -- unchanged.
- Pawn-shield / king-safety scoring -- unchanged, still deferred.
- This keeps the token-budget / effectiveness tradeoff the same shape as
  v1: cheap deterministic tags stay inline (paid every turn, ~free),
  anything requiring real lookup or pattern matching stays behind a tool
  call (paid only when it fires).

## 7. Guardrails (carried over from v1 unless noted)

- Total plan length target stays < 80 words -- the new axes compete for
  the same 3-fragment cap, the cap does not grow.
- Verifier never sees the line (already stripped via
  `split_narrator_steers`); `_PLAN_RULE` in the persona addenda is
  unchanged.
- Fragments name plans, never board tactics.
- The gate is untouched: `recommend_margin_cp` and the repetition veto
  read only `margin` and `repeats`; the new fields never reach it.
- No new numeric thresholds: the detectors use fixed chess geometry
  (python-chess file / rank / square-color bitboards), so no new
  `SV_AI_PLAYBOOK_*` constant is needed. Should one appear during
  implementation it follows that naming and gets an env override.
- ASCII only in source and fragments.

## 8. Spec and tests

Spec: fold the new tags into `docs/ai-playbook-spec.md` (Tags, Fragment
catalog, Combination rules, Guardrails note slot), drop
`bishops=opposite` and IQP from its deferred list, rename the deferred
material entry to `material`. This plan file is deleted in the same
change; the spec is the living document.

Tests (`server/tests/test_playbook.py`, table-driven FENs, pure
functions, no waits, all green before commit):

- Classifier, per predicate: a positive FEN for White, the mirrored FEN
  for Black (checks the `_ours` / `_theirs` flip against `our_color`),
  and one near-miss negative each: IQP with an own c-pawn; IQP whose
  d-pawn is passed; minority shape with unequal pawn totals; opposite
  bishops with a knight on the board; same-colored bishops; passer on
  the d-file; passers on both sides; kings on the same wing; opposite
  kings with a queen off; opposite kings in the endgame bucket; IQP in
  the endgame bucket.
- `bishops`: `opposite_queens` vs `opposite` flips on a queen leaving
  the board; `pawn` and `bishops` still tag in the endgame bucket.
- Rendering: specific tags follow combo / margin in the documented
  order; phase fragment absent outside the endgame when a specific tag
  fires, present when none does; phase still leads in the endgame with
  specific tags after it; the lost / crushing note survives when three
  axis fragments compete (e.g. `lost` + `iqp_theirs` + `opposite` +
  `closed`); existing v1 rendering tests pass unchanged.
- Prompt assembly and verifier-split tests are unaffected (line shape
  is unchanged).

## Resolved questions

1. `MAX_FRAGMENTS` stays 3. A 4th slot adds ~15-20 words and breaks the
   < 80-word target in the worst case (lost note + repetition note).
   Starvation is solved by the phase-fallback rule in section 5, not a
   bigger cap. New fragments are sized like v1's (~15 words).
2. `castling=opposite` uses king squares, not move history (section 3).
   No plumbing: it catches artificial castling and works on a bare FEN.
   It additionally requires both queens on: a pawn race against a king
   with the queens off is not the same plan.
3. Board state alone is right: plans follow from the pawn skeleton, not
   from how it arose, so a transposed IQP is an IQP. The real
   false-positive risk was the loose predicates (isolated e-pawn,
   "roughly symmetric center"), now tightened in section 3; the IQP
   also excludes a passed d-pawn.
4. Ship all 5: each is now a tight, cheap board predicate.
5. `bishops` splits on queens, not on phase: with queens on the theme is
   the attack on the bishop's color in any pawn bucket; the drawing
   tendency needs the queens off. Knights on the board void the tag.
6. The note slot is `axis[:MAX_FRAGMENTS - len(notes)]`, not a separate
   constant: the notes count against the same cap and win.
7. The v2 `imbalance` axis is pawn shape; the deferred material axis is
   renamed `material` in the spec rather than renaming this one.
