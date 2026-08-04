# HvE difficulty levels (target: 0.5.2)

## Goal

Adjustable difficulty in Human-vs-Engine play with any standard UCI
engine -- no engine changes, no non-standard options.

## Approach

Do not weaken the search; weaken the move selection.

1. One normal full-strength search runs on the real clock, the engine
   managing its own time -- byte-identical to max difficulty. No extra
   searches, no `searchmoves`, no probes, no scoring knobs.
2. The search's info stream already reports the best move per completed
   depth iteration. Collect those (move, score, depth) triples
   (skipping aspiration fail-high/low bounds and MultiPV side lines),
   dedup to distinct moves keeping each move's deepest entry.
3. Each candidate's cost behind the final best:
   `max(0, final_cp - cp) + depth_penalty * (final_depth - depth)`.
   The cp shortfall is floored at 0 (shallow optimism the deeper search
   refuted is a mirage, never a bonus); the depth penalty prices each
   depth of shallowness so stale candidates fade at high levels.
4. Sample by truncated softmax over those costs:
   P(m) ~ exp(-cost / temp), cost above the drop cap = excluded.
   - temp = `temp_step * (10 - level)`; temp -> 0 is argmax.
   - cap = `cap_step * (10 - level)`: level 9 tolerates ~50cp of
     cost, level 5 ~250, level 1 ~450.
   - The final best always has cost 0: always eligible, always the
     most likely move.

A shallow candidate's missed refutation is exactly what a weaker
player misses: low levels play the engine's "early impressions",
weaker but never absurd -- every candidate was the engine's best at
some depth, so blatant one-move blunders never happen at any level.
That is deliberate: weakening by search depth is how humans actually
differ in strength; artificial howlers feel fake. The floor is
therefore "shallow but sane", not beginner-random.

## Search Lines panel, arrow, clock

All untouched: the search is the normal one, streamed and charged
exactly as at max difficulty. The sampled move's score (at its depth)
lands in eval history. Level 10 and analysis mode are unchanged.

## Opening book

Book moves play at full strength at every level. Book lines are
prepared knowledge, not over-the-board skill -- humans at every level
play memorized theory accurately and only start erring once out of
book. The player already controls exposure via book depth/off.
Caveat: at low levels a deep book can make the opening feel
disproportionately sharp; the remedy is shortening book plies, not
weakening the book.

## Why this works with any engine

Per-depth `info` lines with `depth`, `score`, and `pv` are the one
thing every UCI engine emits. No `MultiPV`, no `searchmoves`, no
`go nodes/depth/movetime`, no strength-limiting options.

## Known limits

- The candidate pool only has variety where the search changed its
  mind across depths; in forced/obvious positions every level plays
  the same (human-realistic).
- A shallow candidate carries its at-depth score, so the drop cap
  bounds estimated cost, not exact full-strength cost.

## Cost

Zero: the one search that was running anyway.
