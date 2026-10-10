# Studio Watcher Reconnect

## Problem

On mobile, backgrounding the browser or locking the phone suspends the
tab and the OS drops its sockets. Watcher windows (engine and game) close
themselves on any WS `close` (`web/app/tournament-live-game.js`), so on
return the windows are gone.

## Server

- Game streams (`/ws/tournament/game/{pair_id}`): gap. Subscribing to a
  dissolved pair sends an `ended` sentinel with unknown result and no
  snapshot, so a reconnected window would show a stale board under a
  bare "game ended" banner.
- Fix: `_dissolve_pair` records the pair in a dissolved map
  (pair_id -> result, termination, each proxy's final `position` line),
  cleared in `_reset_pairing_state`. `subscribe_to_game` replays the
  recorded positions and then the real sentinel. Memory is a few hundred
  bytes per game; bound with an LRU if needed.
- Engine streams (`/ws/tournament/proxy/{proxy_id}`): gap. Subscribing to
  an exited proxy registers a queue that never receives anything, so a
  reconnected window would sit stale.
- Fix: orchestrator keeps a set of ended proxy ids (filled in
  `proxy_session_ended`, cleared in `_reset_pairing_state`).
  `subscribe_to_proxy` sends the `ended` sentinel immediately when the
  proxy is in that set or no tournament is active.

## Client

- `web/app/ws.js` already implements exponential-backoff reconnect for
  the main socket. Generalize `connect()` to take a URL; watcher windows
  use it instead of a raw `WebSocket`.
- Backoff literals in `ws.js` become named constants.
- Unexpected close => reconnect; the server's snapshot replay repaints
  the board.
- `ended` received => current behavior (game: result banner; engine:
  stop), and stop reconnecting.
- User closes the window => stop reconnecting.

## Tests

- First: failing e2e repro -- drop a watcher's socket, assert the window
  stays open and repaints after reconnect.
- Server: subscribing to an ended proxy, or with no active tournament,
  yields the `ended` sentinel.
- Server: subscribing to a dissolved pair replays the recorded final
  positions, then a sentinel carrying the real result and termination.

## Reload restore (live-test finding)

Mobile may discard the backgrounded tab instead of just dropping its
sockets. The page then reloads and the watch windows are rebuilt from
the saved board list, not reconnected. Observed in Studio: engine
windows came back, finished game windows were gone, no toast.

- Cause: both restore paths drop a saved game board that is neither
  `resolved` (no `game_reconciled` seen before the reload) nor still
  live (`livePairings` no longer maps its proxy to that pair_id).
  Studio `restoreBoards` drops silently; Arena `initWorkspace` counts
  it into the "finished while away" toast. The drop predates this
  work: attaching would have hit a bare `ended` sentinel, and the
  window would have flashed and vanished.
- Fix: while the tournament is running, reattach such a board live
  (`openBoard` / `attachWatch` as for a still-live one). The server's
  dissolved-pair replay now paints the final board and the real result
  (upgraded by reconcile), and the window stays.
- Studio: in `restoreBoards`, the `live` check gates only the
  not-running case; running => always `openBoard`.
- Arena: in `initWorkspace`, the `stillLive` branch becomes
  unconditional under `running`; the toast counts only the not-running
  case. Update the stale comment about auto-close on first `ended`.
  Arena is desktop-only (the workspace closes on mobile), so this is
  for consistency.
- Still dropped: a tournament that finished while away. The server's
  per-tournament state (dissolved map included) is reset on the
  terminal event, so there is nothing to replay. The client-side
  `gameId && currentFen === null` auto-close on `ended` stays as the
  guard for that path.
- Test: e2e per UI -- save a game board, dissolve the pair on the
  server, reload, assert the board is present with the result banner.

## Deferred

- History cap: restore learns a game is resolved from `game_reconciled`
  in the REST event history, a ring of `SV_EVENT_HISTORY_MAX` (200)
  events. Each game emits several, so after a long absence an old
  game's reconcile is evicted; its board reattaches live with the real
  result but no Review button, and the next reload repeats that.
  Fix: `_DissolvedPair` keeps `game_n` (set in `_emit_reconciled`), the
  game sentinel carries it, the live window calls `setReplayGameN` and
  exposes the resolution so Studio/Arena snapshots persist it.
- Opponent name: dissolved-pair replay frames lack `engine_name`, so a
  board reattached live to an unreconciled ended game shows "Black"
  (or "White") for the opponent. Fix: include it in the replay payloads.

## Resolved

- The second `wb.onclose` in `tournament-live-game.js` belongs to the
  frozen (finished-game) window, which has no socket; no change needed.
- The attach-time terminal path in `_stream_queue_to_websocket` now
  flushes queued frames before the sentinel, so the dissolved-pair
  replay reaches the client.
