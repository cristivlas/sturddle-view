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
3. Sample the move by softmax: P(m) ~ exp(score / temp).
   - temp -> 0: argmax, full strength.
   - Higher temp: worse moves gain probability proportional to how
     bad they are -- inaccuracies common, gross blunders rare.
     Feels human, not random.
4. Difficulty slider maps to temperature (optionally also T).

## Clock handling

The scoring sweep runs off-clock. The engine clock starts only for a
cosmetic "thinking" delay after the move is sampled.

## searchmoves fallback

Some engines ignore `searchmoves`. Detect at startup: probe with
`go searchmoves <mv> movetime T` restricted to a single deliberately
bad legal move; a compliant engine must return it as bestmove. Any
other bestmove means unsupported.

Fallback scoring: push each candidate (`position ... moves <mv>`),
run plain `go movetime T`, negate the reply score. Same ranking,
using only `position`, `go movetime`, and `score cp`.

Falling back must be logged.

## Why this works with any engine

`searchmoves`, `movetime`, and `score cp` are core UCI; no reliance
on `MultiPV`, `go nodes/depth`, or strength-limiting options.

## Cost

N legal moves x T per engine move; acceptable at small T.
