# Engine Ratings & Anchored Elo (mini-spec)

Gauge an unknown engine's absolute strength: give reference engines an
approximate rating (e.g. from CCRL/CEGT), run a gauntlet with the test
engine as the seed, and shift the mean-centered ordo fit onto the
references' known scale.

## Registry

- `Engine.rating: int | None` (logistic Elo, optional, default `None`).
- Persisted in `engines.json`; no schema bump. Tolerated absent on load.
- `add()` / `update()` accept it; engines API serializes it.

## UI

- Engine dialog: numeric "Rating" input beside the Name field
  (registry metadata, like name -- not a UCI option, not launch).
- Blank = unset. Clearing the field clears the rating.
- Leave the engine being calibrated unrated (see Anchoring).

## Resolution -- live first, frozen fallback

- `_freeze_engines` snapshots `rating` into the frozen ref (like
  options), but the drift gate does NOT compare it: rating affects
  display only, never play.
- Standings are recomputed from games.pgn on every detail GET; at that
  moment each engine's rating resolves to the live registry value
  (frozen ref id -> entry, name/cmd fallback), else the frozen value
  (engine deleted -- the snapshot is all we have, same rationale as
  frozen options).
- So a post-run rating edit re-anchors a finished tournament on next
  view -- no wipe, no rerun -- and deleting an engine doesn't degrade
  a past tournament's anchor.

## Anchoring (server, pgn_stats)

- Joint fit unchanged (mean-centered, purge rules intact).
- Anchor set = engines with both a fitted `elo_ordo` and a resolved
  `rating` (live-first, frozen fallback). Requires >= 1 such engine.
- `offset = mean(rating_i) - mean(elo_ordo_i)` over the anchor set
  (unweighted; matches ordo pooled-anchor behavior).
- Per engine: `elo_anchored = elo_ordo + offset`; margin unchanged
  (a constant shift adds no variance).
- Applies to any tournament type; gauntlet is the motivating case.
- The test engine must stay unrated, otherwise its assumed strength
  biases the anchor.

## Presentation

- Standings: when an anchor exists, the Ordo column shows absolute
  anchored values (e.g. `3142 +/- 12`) instead of signed relative;
  header/tooltip indicates anchored. No new column (layout risk).
- H2H Elo column, SPRT, fastchess argv: untouched.

## Non-goals

- No fastchess involvement (it accepts no ratings).
- No rating auto-fetch from external lists.
- No per-tournament rating overrides.

## Resolved

- Ordo column switches to absolute when anchored; no extra column.
- Anchored display only when the fit is a single connected component
  (gauntlets always are); otherwise stay relative.
