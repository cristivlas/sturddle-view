# Planner + Verifier Subagent - Design

Branch: `feat/plan+subagents`. Augments the single-agent analysis loop
(`AIAnalysisCoordinator.run()`) with a light planning step and a verifier
subagent. Applies to both personas (coach + commentator), same architecture;
only the base system prompt differs.

## Problem

The single loop conflates two jobs:
- **Narration** -- judgment, short prose budget, voice/length rules.
- **Verification** -- deep search, many tool calls, raw eval numbers.

Cramming both in one context means the narrator marinates in eval noise
(-> engine over-trust) and the prose budget competes with verification rounds.

## Flow per turn

1. **Plan** (no prose): narrator names the 1-3 critical questions/lines for
   this position. Reuses the existing first round, planner-framed. Not a
   persisted todo list -- it's the fan-out decision (which lines get checked).
2. **Delegate**: narrator calls `delegate` once per question. Each spawns a
   verifier sub-run.
3. **Verify** (subagent): own coordinator run, verifier prompt, restricted
   toolset (analyze / top_moves / piece_at; NO recommend_move, NO delegate).
   Must call a tool before issuing a verdict. Returns a one/two-sentence
   live-position conclusion.
4. **Synthesize**: verdicts land as tool_results; narrator writes the 3-5
   sentences, calls recommend_move as today.

## Anti-hallucination model

Two distinct failure modes, two distinct catches:

- **Fabricated tokens** (illegal move, piece not on board): caught by the
  existing validators (`find_illegal_moves` / `find_false_piece_claims` /
  `find_castle_word_violations`) + correction loop. These run on the verifier
  output too -- they are token scanners, structure-agnostic, so terse verdicts
  validate fine.
- **Wrong tactical judgment** ("Nxd4 loses" when it holds): NOT catchable by
  any text validator -- only by the engine. Caught by forcing the verifier to
  call analyze/top_moves before concluding. No tool call -> reject the verdict,
  re-prompt.

Validation layer is the cheap backstop for fabricated tokens; the engine call
is the real check on judgment.

### Why verifier outputs live-position conclusions only

A verifier reasons about positions inside calculated lines. The validators
only know the live board, so any line-internal claim ("after Nxd4 Qh5, the
knight on d4 hangs") would be false-flagged. Resolution: verifier prompt
forbids narrating the line -- it states only the verdict about the move in the
CURRENT position ("Nxd4 loses material; the knight cannot be held after the
recapture"). That conclusion validates cleanly against the live board with the
existing validators, full correction loop, no new infra.

Rejected: structured JSON output from the verifier. Small Ollama models emit
malformed JSON, which would force a json-repair dependency -- tech-debt shim.
Prose + existing validators avoids it entirely.

## Decisions taken

- **Both personas, same arch.** Coach + commentator share the planner+verifier
  flow; only the base system prompt differs.
- **Same provider/model** for verifier as narrator (per-turn provider already
  built in `_ai_kick`). No model-selection plumbing.
- **Full validation on the verifier.** Catch fabricated tokens at the source,
  not only at the narrator's gate -- a plausible hallucinated refutation could
  otherwise be synthesized into prose that passes the narrator's check.
- **Verifier must call a tool before any verdict.** No tool call -> verdict
  rejected, re-prompt.

## Code seam

- Verifier = `AIAnalysisCoordinator.run()` with `mode="verifier"` prompt +
  restricted registry. Factor the loop so a sub-run reuses it but returns text
  instead of streaming `ai_info`.
- `delegate` tool: takes `{question}`, calls a coordinator sub-run, returns
  `{verdict}`. Lives at coordinator level (needs provider + registry refs),
  not in `tools_engine.py`.
- Guards: verifier registry omits `delegate` (one level deep, no recursion);
  verifier gets its own `MAX_TOOL_ROUNDS`.

## Open / TODO

- Latency: each verifier is a full model turn. Measure on live coach mode.
- New tunables get named const + `SV_` env var (verifier round cap, max
  delegate fan-out).
- Verifier prompt mode: new addendum/persona in `prompts.py` (`PromptMode`
  gains `verifier`), or a separate assembly path -- decide at impl.
- Events: does a verifier sub-run surface its tool calls to the UI, or run
  silent? Lean silent (internal), but the existing `ai_tool_call` plumbing
  could show "verifying line X".
