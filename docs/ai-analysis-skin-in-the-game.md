# AI Analysis: Skin in the Game

Problem statement and proposed direction for making AI commentary
concrete and motivated. Companion to docs/ai-analysis-spec.md.

## Problem

Frontier models produce nominal, theoretical, non-actionable chess
prose. Example (Sicilian, 5.Bb5 after e4 c5 Nc3 Nc6 f4 e6 Nf3 d5):

> White plays 5.Bb5 to develop a piece and contest the c6-knight. Unlike
> the fianchetto setups in the Grand Prix Attack family where white
> delays active minor piece play for flank expansion, this direct
> approach challenges black's central control early. The move keeps
> development balanced while preparing kingside safety.

Every sentence is a prompt line echoed back:

- "Unlike the fianchetto setups..." = OPENING_PHASE_GUIDANCE
  ("contrast a sibling line with the current position"). Mandated one
  sentence; the model files it and moves on.
- "keeps development balanced while preparing kingside safety" =
  PHASE_OPENING playbook fragment restated.
- "develop a piece and contest c6" = recommend_move conclusion rule
  ("name the plan the move carries").

## Why the model is unmotivated (structural)

- The prompt pays for compliance, not calculation. Cheapest compliant
  output is vague theory; every concrete claim costs a tool call and
  risks a position-check flag.
- Fixed workflow: one top_moves, one delegate, one recommend_move. A
  verification pipeline, not a search. No idea is generated, tested,
  and discarded.
- Verifier returns holds/refuted plus one line. Narrator never sees the
  refutation, so cannot narrate it.
- Plan: line hands the model a conclusion before it looks at the board.
- 3-5 sentence cap rewards summarizing over showing work.
- Nothing is at stake. The model is never wrong in a way it feels.

Note: enabling extended thinking does NOT fix this (tried). More
private reasoning with the same incentives yields the same prose.

## Where the code says so (llm/prompts.py, llm/playbook.py, play/tools_engine.py)

- SYSTEM_PROMPT_PREFACE: "rather than leaning on the tools to think
  for you". Tells the model not to calculate.
- _PLAN_RULE: the Plan: line "decides the pick ... not the
  highest-scoring one". Plan beats chess strength by design.
- recommend_margin_cp: up to 80cp (behind) of slack lets a weaker
  plan-serving move through.
- _NARRATOR_RULES: 3-5 sentences; "Reasoning stays internal". The
  calculation is forbidden from the output.
- COACH/COMMENTATOR addenda: close on "the plan the move carries";
  RECOMMEND_MOVE card: "one-to-two sentence conclusion".
- VERIFIER_ADDENDUM: "Never narrate the moves inside the line you
  calculated"; verdict is one line.
- top_moves: model supplies the candidates; if it never thinks of the
  best move, nothing finds it (recommend_move only vetoes).
- _SHARED_RULES honesty taxes: pin/fork only via `tactics`, every
  square via `piece_at`, every line via `report_line`. Each concrete
  claim costs calls against a per-turn budget.

## What we want

Goal: the strongest possible move and the reasons it is strongest.
Prose is a by-product of the search, not the product.

The model treats the position as if it had to win it. Ideas are drawn
from analogous openings and structures, not quoted. Each idea is tried
on this board against the engine. Prose narrates the fights: what was
tried, where it broke, what survived, with concrete lines.

Target shape (same position):

> The Grand Prix idea is f5, hitting e6. Here it fails: 5.f5 d4 6.Ne2
> exf5 7.exf5 Qd5 and the f-pawn is loose while d4 cramps White. The
> other Grand Prix idea, Bb5 trading the c6 defender of d4, transfers:
> 5.Bb5 Nge7 6.exd5 exd5 7.d4 and Black's d5 pawn is a target instead
> of a strength. Bb5.

## Constraint: UI behavior unchanged

Prose content is free to change; that is the point. Every UI surface
that fires today must still fire, with the same shape:

- `ai_recommendation` (events.py): links the pick in prose to its
  verified line; board arrow follows. Fires on every accepted pick,
  fallback picks included, with `pv_uci`. Prose must name the accepted
  move so the link lands.
- recommend_move progress steps `engine_best` / `search_move`: drive
  the board arrow and Search Lines during the check. Withholding the
  engine's best from the model (3a) must not withhold it from the UI.
- Tool searches publish `engine_info` (PV window, board arrow).
  play_out and test_aim search through the same path as `analyze`
  (search_cache + publish), so they light the UI the same way.
- `ai_opening_links`: opening names in prose stay linked; related_openings
  stays called in opening turns.
- Tool panel rows, verdict badges (delegate holds/refuted), struck
  prose on position-check flags: unchanged. New tools ride the same
  row mechanism as existing ones.
- Book-reply turns: submit the book move directly, as today. The
  play_out requirement (3) does not apply to them.

## Proposed direction

Core: the model is a player, not a narrator. It plays its ideas out
against the engine and is scored on the result.

0. Reframe the prompt. Preface: "find the best move by calculation;
   use the tools to calculate". Drop _PLAN_RULE's "decides the pick";
   the best-scoring move that survives play_out wins. Lift "Reasoning
   stays internal" for lines: the calculation IS the output.
   VERIFIER_ADDENDUM rewritten per 7.
1. Idea generation. The model must name 2-3 concrete ideas before any
   prose, each a first move plus its aim: a move the idea works toward
   within SV_AI_AIM_HORIZON of its own moves (e.g. 5.Bb5, aim d4). In the opening, ideas come from
   related_openings sibling lines (borrowed, adapted, never quoted).
   Commentator: the move played is always one of the ideas. The 2-3
   count is prompt-level, not enforced: a forced recapture needs one.
2. Simulation tool: `play_out(idea, move)`, stepwise. Model plays one
   move for its side; engine answers for the opponent with its best
   reply (deterministic; model-chosen replies are the verifier's job,
   7). Result: the reply, the new FEN, eval before/after and the
   step's drop. Never the engine's best for the model's own side: no
   bestmove, no PV past the reply. Model sees each reply before
   choosing its next move (no pre-scripted sequences: they break once
   the engine diverges). Server keeps one line per idea; the first call
   with a new idea starts from the live position; model ends an idea
   by stopping. A failed step (3) is not kept: the line stays where it
   was, so the next call with that idea tries another move. Default
   analyze depth; one search per ply (the post-move search yields the
   reply, the post-reply search the next eval-before). Ideas are independent, so steps of different ideas
   can share a round. This is the "skin in the game": the idea is
   played, not asserted.
3. Scoring. A step's drop is the eval before the model's move minus
   the eval after it, from the moving side's POV, so a failure is pinned to the
   move that caused it. Drop beyond SV_AI_PLAY_OUT_MARGIN (own knob,
   default the current 50cp midpoint: a 20cp margin is inside
   depth-to-depth eval noise and would fail sound steps) = step
   failed; the result says so and tells the model to replace that move
   or drop the idea. "Played out" = a first step with that move ran,
   failed or not; dominance then decides.
   recommend_move rejects a move not played out (naming the rule, not
   a move), then judges it by dominance (3a) alone: a bad follow-up
   never vetoes a good first move. Dominance margin tightens to MIN regardless
   of standing: the plan no longer buys slack. The side-ahead
   repetition ban stays.
3a. Missed-move check: the existing dominance check, name withheld.
   recommend_move rejects only when the engine's best beats the
   submitted move by more than the margin, and says "a stronger move
   exists" without naming it. A move within margin is accepted even
   if the engine's pick was never played out: in quiet positions the
   engine's top choice is near-arbitrary and not worth a rejection.
   The model keeps hunting with its own ideas; the engine stays a
   referee, not a hint. (Naming it would collapse the design into
   playing out the engine's pick, the over-trust the registry split
   removed.) The model-facing result drops `engine_best_move` /
   `engine_best_san`; the progress steps still carry them to the UI.
   Same for top_moves: its model-facing entries drop `pv` (score
   only), else the model copies the engine's continuation into
   play_out and plays nothing of its own. engine_info still carries
   the PV to the UI.
3b. Stalls. The MAX_RECOMMEND_FAILURES nudge retargets from "rank with
   top_moves" to "play out a new idea". Fallback: when
   SV_AI_FALLBACK_ROUNDS_LEFT rounds remain (default: enough for the
   submit plus the prose round), the server nudges the model to submit
   now, and recommend_move accepts any played-out move unconditionally
   (a missed mate included; the red_team_first hold is skipped), flagged
   `weaker_than_best` when dominance would have rejected it, so the
   prose owns it as the practical choice. A submission not played out
   is replaced by the played-out first move with the best eval after
   its first step. Like a player out of clock: the best move found
   gets played. Nothing played out, or the nudge ignored: the turn
   ends with no recommendation (today's round-cap behavior). The
   engine's best stays unnamed throughout.
4. Narration from the record. Prose budget: one short paragraph per
   idea played, concrete line each, plus the pick. Lines come from
   play_out results so report_line is implied. The 3-5 sentence cap
   goes; sentence-level square-naming rule goes (lines satisfy it).
   Coach: lines are analysis, not the opponent's intent; the rule
   becomes "never claim to know what the opponent will play".
5. Plan: line removed from the first message. Available as a tool
   (`plan()`) the model may consult; input, not answer.
6. OPENING_PHASE_GUIDANCE rewritten: "borrow one idea from a sibling
   line, play it out here, report whether it transfers and why".
7. Verifier/delegate: attacks the idea's aim, not its first move
   (play_out already proved move 1 survives the engine's reply).
   `delegate(move, aim, question)`. The verifier's tool
   `test_aim(reply)`: server plays the move and the verifier's reply,
   then the free line is one search's PV, read for SV_AI_AIM_HORIZON
   (default 3) narrator moves (a shorter PV shortens the horizon). At
   each narrator move on it where the aim is legal, a branch search
   forces the aim and scores it against the PV's eval; the best
   attempt counts. Forcing it at the first
   chance only would refute sound aims that need preparation (5.Bb5
   Nge7 6.d4?? cxd4, where the aim comes after 6.exd5 exd5). Searches
   at verification depth. A reply counts only if sound: within the
   dominance margin (MIN) of the opponent's best reply, else the
   result says "unsound reply". Refuted = after a sound reply, the
   aim is legal at none of the horizon's moves, or its best attempt
   falls short of the PV's eval by more than SV_AI_PLAY_OUT_MARGIN
   (the branch and the PV root search different depths at that node,
   so MIN would be noise). Up to SV_AI_VERIFIER_MAX_REPLIES (default
   3) replies per delegate, within VERIFIER_MAX_ROUNDS; one
   refutation suffices; "holds" otherwise.
   The verdict gate's dominance confirmation is replaced: "refuted"
   stands only when a test_aim result in that sub-run refuted, and the
   verdict cites that line in SAN. The verdict's board check exempts
   moves inside that replayed line (8), or every cited line would be
   withheld as a board misstatement. The narrator must narrate the
   refutation and either justify the move from its play_out record or
   drop the idea; a new aim is a new claim and needs a new delegate. The
   move itself still stands if 3a passes it. red_team_first hold and
   the holds/refuted badge row unchanged; the hold's nudge drops "once
   the verdict holds" (a refuted aim no longer blocks the move).
8. Guardrails stay post-hoc (position check, tool leaks). Pre-emptive
   bans that discourage concreteness (pins only via tactics, every
   line via report_line) relax when the claim is inside a play_out
   or test_aim line already replayed by the server.

## Open questions for review

- Budget: ideas x plies + delegate + recommend must fit
  SV_AI_MAX_TOOL_ROUNDS (default 32). Needs caps
  (SV_AI_PLAY_OUT_MAX_PLIES, SV_AI_MAX_IDEAS) sized from live turns.
  Each test_aim is up to 2 + horizon searches at verification depth:
  the opponent's-best search (shared across replies, cached), the
  reply's PV search (its root eval is the soundness check), one forced
  branch per narrator move: 5 at the default horizon. x3 replies x
  delegates per turn; measure engine time on live turns.
- Does the score need teeth beyond rejection? e.g. a running record
  across turns ("2 of 3 ideas failed this game") fed back to the
  model. Cheap to add; unclear if it changes behavior.
- Position check vs multi-position prose: narrating several lines
  from one board will trip single-board flags. The judge handles
  "other position" references today; verify it scales.
