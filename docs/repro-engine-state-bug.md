# Reproduction: engine state-sync bug (assert self._engine is not None)

Symptoms observed in the wild:
- `_think_and_play` raised `AssertionError` at the entry assert
  (`self._engine is not None and self._board is not None and self._game_id is not None`).
- Engine clock ticked down (engine's turn) but no `engine_info` events arrived.
- UI showed stale engine info from a previous search.
- Game ended with `timeout, loser=white` (engine flagged).

## Move sequence

White (human, ?), Black (engine, ?). Reconstructed from the move list in the UI:

```
1. e4    e6
2. d4    Nc6
3. d5    Bb4+
4. c3    Bc5
5. dxc6  Bxf2+
6. Kxf2  Qh4+
```

After 6...Qh4+ it is white-to-move (the engine's turn). At that point the
search task entered `_think_and_play` and immediately failed the entry assert,
meaning `self._engine` was `None`.

Likely cause: a take-back or new-game between moves zeroed `self._engine` via
`_cancel_think()`, and the next `_engine_to_move()` got scheduled before
`_ensure_engine()` had a chance to re-spawn the subprocess. We are racing the
respawn against the search.

## What to test

1. Reproduce by replaying the moves above with a take-back interleaved at any
   point between moves 3-5 and the start of move 6.
2. Verify (post-fix) that:
   - `_engine` is always non-None at `_think_and_play` entry, OR
   - `_think_and_play` is robust to a `None` engine and re-spawns instead of
     asserting.
   - Engine produces `engine_info` events for the new search.
   - Clock for the active side ticks correctly.
