# HvE difficulty levels (target: 0.5.2)

## Goal

Adjustable difficulty in Human-vs-Engine play with any standard UCI
engine -- no engine changes, no non-standard options.

## Model

Weaken the engine's candidate vision, not its calculation. A weaker
human doesn't calculate badly so much as fail to CONSIDER the best
moves; whatever they do play, they play with intent. Accordingly:
below max difficulty the engine is blinded to some of its best
options, then calculates normally among what it still sees.

## Approach

Level 10: business as usual, one unhindered search.

Below max, per engine move:

1. Off-clock shallow sweep: score every legal move with
   `go searchmoves <mv>` at a short fixed movetime (default 100ms).
   These scores only gate visibility -- they never choose the played
   move, so their shallowness cannot produce a dud move directly.
2. Pool admission, two gates per move:

   Auto-range: normalize each gap by the position's own score spread,
   `g = (best - score) / (best - worst)`, so g is 0 for the best move
   and 1 for the worst. Moves with `g <= (10 - level) / 10` pass:
   level 9 admits only the top tenth of the range, level 1 nearly all
   of it.

   Win-prob cap, all levels: sweep cp maps to win probability via a
   logistic, `wp = 1 / (1 + exp(-cp / scale))` (scale ~180cp, an
   empirical engine fit), and a move passes only if
   `wp(best) - wp(move) <= drop cap` (default 0.15). Near equality
   that is ~110cp, so no level hangs a piece from a healthy position;
   the sigmoid flattens away from zero, so the same cap self-loosens
   when already behind and a losing side keeps a wide pool -- low
   levels stay weak instead of rubber-banding to strength. The clamp
   only folds mates and huge evals to a finite cp -- the worst legal
   move still sets the auto-range denominator.
3. Blinding pass, one Bernoulli roll per pool move: removal odds
   decay linearly across the admitted band,
   `q = qmax * (1 - g / width)` with `width = (10 - level) / 10` and
   `qmax = removal_step * (10 - level)`. Lower level: better moves
   more likely removed, and removal reaches deeper into the ranking.
   If every move is removed, the best survivor is retained -- the
   pool is never empty.
4. The real move: `go searchmoves <pool>` with the normal clock
   fields, on-clock, the engine's own time management. The engine
   plays its best VISIBLE move at full depth.

## Clock handling

The shallow sweep runs before the engine's clock starts (off-clock),
consistent with it being our bookkeeping, not engine thinking. The
restricted search is honest: real clock fields, real time consumed,
commit debits real elapsed and credits the increment. The human clock
is untouched; level 10 is fully unchanged.

## Search Lines panel

The restricted search is a real search streamed through the normal
info pump: Search Lines, the board arrow, and eval history all come
from it, exactly as at full strength. The shallow sweep publishes
nothing (the panel clears at search start as usual).

## searchmoves detection

Both the sweep and the restricted search need `go searchmoves`.
Detect lazily -- once per engine process, on its first
difficulty-limited move (self-healing across respawns): probe
restricted to a single deliberately bad legal move in a mate-in-1
position; a compliant engine must return it as bestmove.

Probe position: `6k1/5ppp/8/8/8/8/5PPP/R5K1 w - - 0 1` (back-rank
mate Ra8#), probing `go searchmoves a1a2`: reply a1a2 = restriction
honored; anything else = ignored. Mate-in-1 makes the unrestricted
choice predictable at any depth.

On failure the feature degrades loudly: the game plays at full
strength, the failure is logged, and a toast tells the user
difficulty is unavailable for this engine (once per engine process).

## Opening book

Book moves play at full strength at every level. Book lines are
prepared knowledge, not over-the-board skill; the player already
controls exposure via book depth/off. At low levels a deep book can
make the opening feel disproportionately sharp; the remedy is
shortening book plies, not weakening the book.

## Knobs (SV_ env)

| Knob | Default | Role |
|---|---|---|
| `SV_HVE_SWEEP_MOVETIME_SECONDS` | 0.1 | shallow sweep movetime per move |
| `SV_HVE_REMOVAL_STEP` | 0.10 | qmax per level below max |
| `SV_HVE_SCORE_CLAMP_CP` | 1000 | mate folding / cp clipping before ranging |
| `SV_HVE_WINPROB_SCALE_CP` | 180 | logistic scale for cp -> win prob |
| `SV_HVE_WINPROB_DROP_CAP` | 0.15 | max win-prob drop vs best for admission |

## Why this works with any engine

`searchmoves`, `movetime`, clock-based `go`, and `score cp` are core
UCI. Engines that ignore `searchmoves` degrade to full strength with
a visible notice.

## Cost

N x 100ms off-clock per engine move (~2-4s), plus one normal think.
The played move is always a full-depth choice; expected strength is
set by what the engine is allowed to see, not how well it thinks.
