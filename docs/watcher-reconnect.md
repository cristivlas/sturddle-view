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

## Resolved

- The second `wb.onclose` in `tournament-live-game.js` belongs to the
  frozen (finished-game) window, which has no socket; no change needed.
- The attach-time terminal path in `_stream_queue_to_websocket` now
  flushes queued frames before the sentinel, so the dissolved-pair
  replay reaches the client.
