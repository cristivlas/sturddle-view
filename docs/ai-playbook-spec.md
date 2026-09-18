# AI Playbook - Spec

Situation-dependent strategy for the AI analysis agent's pick.
Companion to `docs/ai-analysis-spec.md` (§Future skills points here).
Shipped: classifier `play/playbook.py`, fragments `llm/playbook.py`,
plan line wired in `_ai_kick._playbook_for`, gate in
`tools_engine.make_recommend_move_tool` (`app._ai_situation_provider`).

## Goal

The playbook drives the move the agent settles on; the narration follows
from that move. Winning big plays differently from losing big, an endgame
from an opening, a closed structure from an open one: the server
classifies the position, hands the model the plan that fits, and tunes
the engine gate so a plan-fitting move survives it. The model chooses
candidates that carry the plan out; the engine only vetoes blunders.

## Vocabulary

- **Playbook** -- the whole feature: classifier + fragment catalog.
- **Situation** -- the set of tags the classifier derives for a position.
- **Tag** -- one classified fact, e.g. `phase=endgame`, `margin=winning`.
- **Fragment** -- the short prompt text keyed on a tag (or tag combo).

## Architecture

### Classification is server-side and deterministic

- Pure function of (board, eval, our side) using python-chess. No LLM
  tool call, no engine search of its own.
- Lives in `server/sturddle_view/play/playbook.py`; returns a
  `Situation` (frozen dataclass of tags).
- Thresholds are named module constants with `SV_` env overrides (project
  rule), never literals.

### Injection: the `Plan:` line on the initial user message

- Fragments ride the initial user message as one `Plan:` line
  (`PLAYBOOK_LEAD`), after the position context, exactly like the
  existing opening steers (`OPENING_PHASE_GUIDANCE`,
  `BOOK_REPLY_GUIDANCE` in `llm/prompts.py`).
- Both persona addenda carry `_PLAN_RULE`: the plan decides the pick --
  candidates for `top_moves` carry it out, and among moves the check
  accepts the model submits the one that serves the plan, not the
  highest-scoring one. The closing prose names how the move serves it.
- Not the system prompt: the situation changes every ply; the cold prompt
  stays stable / cacheable.
- Narrator-only. `split_narrator_steers` strips the plan line for
  verifier sub-runs (the verifier judges soundness, not the plan).
- Wire-in point: `_ai_kick._build_turn_inputs` -> classify ->
  `build_initial_user_message(playbook=...)`.

### Gate: `recommend_move` is plan-aware

The engine check would otherwise veto the plan: a complicating or
delaying try scores below the top line by design. Two rules, both from
the side to move's Situation (`app._ai_situation_provider` ->
`HumanVsEngine.situation(board.turn)`):

- Dominance margin by margin bucket (`recommend_margin_cp`): ahead
  (`better`+) -> `RECOMMEND_MARGIN_MIN_CP` (convert cleanly, the engine
  line rules); behind (`worse`-) -> `RECOMMEND_MARGIN_MAX_CP` (practical
  chances beat the top line); even or unclassified -> the midpoint.
  Mate scores ignore the margin as before.
- Repetition veto: a side ahead that submits a move in `repeats` is
  rejected before any search (`recommendation_rejected`, reason names
  the move). Behind, a repeating move goes through the normal margin.

The end-of-turn verifier search (`ai_recommendation`) is unchanged: it
reports the engine's view of the accepted pick, it does not re-gate it.

### Mode scoping

- **Coach** (play mode): "our side" = the human (`_human_white`).
  Fragments are second person ("you are clearly ahead: ...").
- **Commentator** (view mode): no "our side". Margin tags are taken from
  the side to move; fragments are neutral third person ("White, well
  ahead, should ..."). Resign advice never fires here.
- Matches the eval POV convention: coach = human POV, view = side to
  move.

### Eval source for the margin tag

- One accessor, `HumanVsEngine.situation(our_color)`, classifies the
  position under review; its eval is the viewed game's eval at the cursor
  (PGN / recorded eval history) when a game is under review, else the
  live game's latest recorded engine eval.
- No eval at hand -> material balance as a coarse proxy, tagged
  `margin_source=material` so the fragment can hedge ("by material").
- Raw eval numbers never enter the fragment; only the bucket. Keeps the
  narrator's raw-eval firewall intact (see ai-analysis-spec §Planner +
  verifier subagents).

## Tags (v1)

### `margin` -- from our side's point of view

| Tag | Condition (cp, our POV) |
|-----|-------------------------|
| `lost`     | <= -RESIGN_CP or mated-against |
| `losing`   | <= -DECISIVE_CP |
| `worse`    | <= -EDGE_CP |
| `even`     | otherwise |
| `better`   | >= EDGE_CP |
| `winning`  | >= DECISIVE_CP |
| `crushing` | >= RESIGN_CP or mate-for |

Starting values (constants, tune live): EDGE_CP 100, DECISIVE_CP 300,
RESIGN_CP 900.

### `phase` -- pawn-count buckets of 4 (as in the engine)

| Tag | Pawns on board |
|-----|----------------|
| `opening`    | 13-16 |
| `middlegame` | 9-12 |
| `late`       | 5-8 |
| `endgame`    | 0-4 |

The existing `in_opening` (ECO ply + slack) stays the trigger for the
opening-theory steer; `phase=opening` is the strategic bucket. Both can
hold at once.

### `structure` -- open vs closed

- Locked pawn pairs: pawns facing each other on a file, blocked.
- Open files: no pawns; half-open: one side's pawns only.
- `closed`: locked pairs >= CLOSED_MIN_LOCKED and open files <=
  CLOSED_MAX_OPEN_FILES.
- `open`: open + half-open files >= OPEN_MIN_FILES.
- Neither -> no structure tag (no fragment).
- Never tagged in `phase=endgame`: few pawns make every file open and
  structure advice is middlegame talk.

### `repeats` -- repetition available

- SAN of every legal move after which the position has already occurred
  in the game (`repeating_moves`, python-chess `is_repetition(2)` on the
  board's move stack). Empty for a bare FEN.
- Feeds both the plan line (repetition note) and the gate (veto ahead).

### Tags deferred (not v1)

- `king_safety` (castled / open lines near king / pawn shield)
- `bishops=opposite` (opposite-colored bishops, no other minors)
- `imbalance` (exchange up/down, minor vs pawns, queen vs pieces)
- Named position types (IQP, Lucena, Philidor) -- the original
  future-skills item; needs a pattern library.

## Fragment catalog (first draft)

One or two sentences each. Named string constants. Voice is adjusted per
mode by a coach/commentator variant, not by string surgery.

### Margin

- `crushing` / `winning`: convert cleanly -- simplify, trade pieces not
  pawns, keep the king safe, avoid needless complications. Creativity is
  affordable but not required.
- `better`: keep pieces on, improve the worst-placed piece, no rush;
  do not cash in the edge for a simplified but drawn position.
- `worse`: solidify first; trade off the opponent's most active piece;
  trade queens if under attack.
- `losing`: seek counterplay and practical chances; prolong the game --
  no simplifying trades, keep pieces and tension on, make the opponent
  prove the win. (Complicating is the `losing` + `open` combo; in a
  closed position the same margin keeps it closed.)
- `lost` (coach only, phase != opening): the position is objectively
  lost; name the resignation option once, without insisting, and still
  give the most stubborn try.

### Repetition note

Appended after the capped fragments whenever `repeats` is non-empty and
the margin is not `even`, naming the moves:

- behind: a repetition is available; repeating is the drawing try --
  take it unless a move clearly improves.
- ahead: do not repeat the position; make progress.

### Phase

- `opening`: development, center, king safety before adventures; no
  early queen sorties.
- `middlegame`: pawn breaks, piece activity, weak squares, plan before
  tactics.
- `late`: trade toward a favorable ending when ahead; keep tension when
  behind; watch for the transition.
- `endgame`: king activity, create and push passed pawns, promotion is
  the goal; opposition and zugzwang in pawn endings.

### Structure

- `closed`: maneuver, prepare pawn breaks, knights over bishops, play on
  the wing where you have space.
- `open`: bishops over knights, files and diagonals, initiative and
  tactics; tempo matters.

### Combination rules

- One tag per axis (margin, phase, structure); axes are independent.
- Default: additive, one fragment per axis, at most 3 in v1.
- Within an axis the stronger tag wins: `lost` over `losing`, `crushing`
  over `winning`.
- Ordering: phase fragment first in `endgame`, margin first otherwise,
  structure last.

### Combo overrides

Some pairs contradict when added ("complicate" + "maneuver quietly").
A combo table keyed on a tag pair supplies one fragment that replaces
both axes' fragments; the third axis still adds. Lookup order: combo
first, then per-axis fallback. First draft:

| Combo | Fragment gist |
|-------|---------------|
| `losing` + `closed`   | keep it closed, avoid opening lines for the stronger side; wait for a mistake, prepare one break |
| `losing` + `open`     | complicate now: activity and initiative over material; open lines toward the enemy king |
| `winning` + `closed`  | do not force it; prepare the pawn break on the side you have space, then open at the right moment |
| `winning` + `open`    | trade pieces, keep pawns, hold the open files; simplification is the win |
| `winning` + `endgame` | king to the center, create the passed pawn, promote; trade pieces not pawns |
| `losing` + `endgame`  | king activity and counterplay; seek the drawing fortress or opposite-color bishops before it is too late |
| `worse` + `opening`   | finish development first; no pawn grabs; castle and consolidate |
| `better` + `opening`  | keep developing; do not cash in early; punish the lag later |

`lost` and `crushing` take the `losing` / `winning` combo rows plus their
own margin fragment (resign note / convert note) appended.

## Grounding

- Fragments name plans, never concrete tactics on the board; the model
  must ground any "pin" / "fork" it writes with the `tactics` tool
  (ai-analysis-spec §Tactical grounding). Same shared core
  (`play/tactics.py`) is available to future tags such as `king_safety`.

## Guardrails

- Fragment count cap: PLAYBOOK_MAX_FRAGMENTS (3); the repetition note
  rides outside it (concrete moves the gate also acts on).
- Fragments steer the pick, never instruct the model to skip tools or the
  red-team hold (unlike `BOOK_REPLY_GUIDANCE`).
- Verifier never sees them; the gate's margin never widens past
  `RECOMMEND_MARGIN_MAX_CP`, and mate scores ignore it.
- Total plan length stays small (target < 80 words) so it does not
  crowd out the position itself.

## Testing

- Classifier: table-driven FEN -> tags tests (pure function); `repeats`
  from a move stack.
- Prompt assembly: fragment presence per tag; verifier split strips the
  plan line.
- Gate: margin per bucket (`recommend_margin_cp`), the tool accepting a
  behind-margin move it rejects when even, the repetition veto ahead.
- No byte-stable prompt tests (project rule); assert on tag -> fragment
  identity, not exact text.

## Open items

- Phase refinement beyond pawn count (queens off, piece count) if pawn
  buckets misclassify live.
- Structure thresholds need live tuning; start conservative (fewer
  fragments beats wrong ones).
- Whether weaker local models need longer fragments than Claude.
- UI: none in v1. Possibly a settings toggle later if it proves noisy.
