# HvE difficulty levels (target: 0.5.2)

## Goal

Adjustable difficulty in Human-vs-Engine play with any standard UCI
engine -- no engine changes, no non-standard options.

## Approach

Do not weaken the search; weaken the move selection.

1. Enumerate legal moves with python-chess.
2. Score each candidate: `go searchmoves <mv> movetime T`, read
   `score cp` from the final info line. Scores need only be roughly
   right; T stays small (50-100ms).
3. Sample the move by truncated softmax: P(m) ~ exp(score / temp),
   with a hard drop cap.
   - temp -> 0: argmax, full strength.
   - Higher temp: worse moves gain probability proportional to how
     bad they are -- inaccuracies common, gross blunders rare.
     Feels human, not random.
   - Drop cap: moves more than `cap_step * (10 - level)` cp behind
     the best are excluded outright. This makes blunder magnitude
     provably inverse to level (softmax alone can only make big
     blunders rare, never impossible): level 9 never drops more
     than 50cp behind its best option, level 5 never more than 250,
     level 1 never more than 450. Always relative to the best
     available move -- in an already-lost position every option may
     still lose.
4. Difficulty slider maps to temperature and drop cap (both scale
   linearly with levels below max).

## Search Lines panel

During the levels 1-9 sweep the panel shows the scan: one row per
candidate (the Depth column doubles as a 1..N counter), Eval = the
candidate's true score, PV = its line (searchmoves path: the engine's
line; fallback: candidate + reply line). The board arrow hops across
candidates as they are scored. The sampled move's score lands in eval
history. Level 10 and analysis mode are untouched.

## Opening book

Book moves play at full strength at every level. Book lines are
prepared knowledge, not over-the-board skill -- humans at every level
play memorized theory accurately and only start erring once out of
book. The player already controls exposure via book depth/off.
Caveat: at low levels a deep book can make the opening feel
disproportionately sharp; the remedy is shortening book plies, not
weakening the book.

## Clock handling

The scoring sweep runs off-clock. The engine clock starts only for a
cosmetic "thinking" delay after the move is sampled.

Mechanics: the clock is a stopwatch debited on move commit. The sweep
resets it before every candidate search, so at no point is more than
~one movetime on the clock -- no visible countdown or flag fall
mid-sweep. After sampling: final reset, cosmetic delay (capped at half
the engine's remaining time so it can never flag), commit debits the
delay and credits the increment. The human clock is untouched; the level-10 path
is fully unchanged. Fairness by design: difficulty comes from move
sampling, not time pressure, so sweep infrastructure time is
deliberately free and the engine's clock stays honest.

## searchmoves fallback

Some engines ignore `searchmoves`. Detect lazily -- once per engine
process, on its first sweep move (self-healing across respawns):
probe with `go searchmoves <mv> movetime T` restricted to a single
deliberately bad legal move; a compliant engine must return it as
bestmove. Any other bestmove means unsupported.

Probe position: detecting "ignores searchmoves" needs a position where
the unrestricted choice is predictable and different from the probe
move. Mate-in-1 gives that: every engine, at any depth, plays the mate
unrestricted. Concretely, `6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1`
(back-rank mate Ra8#), probing `go searchmoves a1a2`: reply a1a2 =
restriction honored; anything else = ignored.

Fallback scoring: push each candidate (`position ... moves <mv>`),
run plain `go movetime T`, negate the reply score. Same ranking,
using only `position`, `go movetime`, and `score cp`.

Falling back must be logged.

## Why this works with any engine

`searchmoves`, `movetime`, and `score cp` are core UCI; no reliance
on `MultiPV`, `go nodes/depth`, or strength-limiting options.

## Cost

N legal moves x T per engine move; acceptable at small T.
