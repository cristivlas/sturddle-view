# ordo-style joint Elo fit

Status: implemented.

## Why this exists

The standings table reports two Elo numbers per engine:

- **`elo` (logistic):** per-engine `elo_from_score(score_pct)`. Matches
  fastchess's stdout summary, cutechess, and published CCRL/CEGT
  ratings -- the standard "Elo from score" convention.
- **`elo_ordo` (joint fit):** mean-centered rating from a joint
  iterative fit over the full game graph, replicating the output of
  ordo (https://github.com/michiguel/Ordo).

The two numbers are equivalent up to convention:

- 2-engine tour, A scores 52%: `elo = +14 / -14`, `elo_ordo = +7 / -7`.
  Both express the same +14 Elo head-to-head gap; ordo splits the gap
  symmetrically around the pool mean.
- Multi-engine round-robin / gauntlet: `elo_ordo` is the only honest
  cross-engine rating (`elo` for non-leader engines in a gauntlet is
  head-to-head vs the leader, not a pool-wide rating).

Showing both lets users cross-check against an external ordo run
without leaving the app, while keeping the standard logistic Elo as
the primary number.

## Algorithm

Implements Ballicora's iterative fit verbatim (`rating.c::adjust_rating`):

1. For each engine, compute the expected score under current ratings:
   `wperf = N_played * 1 / (1 + exp((r_opponent - r_engine) * BETA))`
   where `BETA = 1 / 175.25` (ordo's default, calibrated so a 202-Elo
   gap yields 76% expectancy).

2. Step each rating toward closing the gap between obtained and expected
   score, with a saturating multiplier:
   `step = delta * sign(d) * |d| / (kappa * played + |d|)` where
   `d = obtained - expected`, `kappa = 0.05`, initial `delta = 200`.

3. Mean-center the ratings.

4. If the global score deviation `sum (expected - obtained)^2` did
   not improve, halve `delta` and continue. Stop when `delta < 0.001`
   or 80 halvings elapse.

White advantage is fixed at 0 (we do not currently estimate one). This
matches ordo's default when run without `-W`.

Mean centering is over the *post-purge* pool: engines with all wins or
all losses (against the rest of the pool) are excluded, matching ordo's
`-G` purge behavior. Disconnected components of the match graph are fit
independently, each anchored to its own mean.

## Margins

95% Wald confidence intervals from the Fisher information of the
binomial likelihood:

- Per game between engines i, j with expected white-score `p`:
  `Info_ii += BETA^2 * p * (1-p)`, similarly for `Info_jj`,
  `Info_ij -= BETA^2 * p * (1-p)`.

- Reduced (n-1) x (n-1) info matrix (last engine dropped to fold in
  the mean-zero constraint), inverted, diagonal = variances of the
  remaining ratings. The last engine's variance comes from the
  constraint `r_{n-1} = -sum others`.

- ordo uses a bootstrap simulation (resampling games, refitting) for
  its CIs. The Wald form runs roughly 1.5x wider than ordo's bootstrap
  CI on the same data. We do not replicate the bootstrap because the
  refitting cost would scale `compute_standings` from ~30ms to seconds
  for a typical 1000-game tour. The asymptotic equivalence makes the
  Wald number a more-conservative-but-equally-valid cross-check.

## Verification

Cross-checked against `ordo -p <pgn> -a 0 -M -D` on the following
test PGNs (in `c:/Users/crist/Projects/games-data/`):

| File | Engines | Ratings | Match |
|---|---|---|---|
| `tou3r.pgn` | 2 | +2.47 / -2.47 vs ordo +2.5 / -2.5 | yes (rounding) |
| `tour-2.pgn` | 2 | +1.56 / -1.56 vs ordo +1.6 / -1.6 | yes |
| `tour.SkylarkII.pgn` | 2 | +7.81 / -7.81 vs +7.8 / -7.8 | yes |
| `tour.pgn` | 3 | +39.88 / -15.32 / -24.56 vs +39.9 / -15.3 / -24.6 | yes |
| `self-full.pgn` | 2 | +4.93 / -4.93 vs +4.9 / -4.9 | yes |
| `self-01.pgn` | 6 (1 purged) | matches ordo for remaining 5 | yes |

Margins run 1.4--1.8x wider than ordo's bootstrap CI, as documented above.

## Performance

`compute_standings` on a 2225-game / 2-engine PGN: ~30ms cold, ~1ms warm.
On a 28-engine / 168-game PGN (`kiwi.pgn`): ~12ms cold, ~6ms warm. The
joint fit adds <1ms to compute_standings since the PGN parse dominates.

## Code layout

- `_ordo_iterative_fit(engines, encounters)` -- pure fit; assumes one
  connected component and no purging.
- `_ordo_fit_margins(engines, encounters, ratings)` -- Wald CI from
  Fisher info.
- `_ordo_connected_groups(engines, encounters)` -- Union-Find on the
  match graph.
- `ordo_fit(engines, encounters, *, wins, losses)` -- top-level: purges
  all-wins/all-losses engines, splits into components, fits each.
- `compute_standings` -- builds the encounter list, calls `ordo_fit`,
  populates `EngineRecord.elo_ordo` / `elo_ordo_margin_95`.

## What was considered and rejected

- **Bayesian / draw-aware MLE** with separate draw parameter (Davidson /
  Rao-Kupper model): produces ratings that differ from ordo's by ~30%
  in magnitude on draw-heavy data. ordo does **not** do this -- its
  rating fit treats each game as a binomial on score (W + 0.5*D),
  separate from the draw-rate estimation. The Davidson model is
  mathematically sound but does not match the reference implementation
  users cross-check against.

- **Bit-exact match of ordo's bootstrap CI**: would require running
  the fit on 100--1000 resampled game sets. Cost scales the standings
  endpoint from ~30ms to seconds. Wald CI is asymptotically equivalent
  and ~1.5x wider; the cross-check use case ("are we on the same scale
  with the same significance") still works.

- **Switching the primary `elo` column to mean-centered**: would change
  long-established display numbers and break consistency with the
  rest of the chess engine ecosystem (fastchess stdout, CCRL ratings).
  The dual-number approach keeps both worlds.
