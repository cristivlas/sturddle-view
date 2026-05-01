# UI Bugs — Tournament Game View

Reported 2026-04-30.

---

## Bug 1 — Stop button not debounced ✅ fixed

**Symptom:** Clicking Stop sends the POST but leaves the button clickable/unstyled during the async wait.

**Fix:** Disable the button and swap its icon for `<wa-spinner>` immediately on click. `loadList()` re-renders the row so no manual restore is needed.

**File:** `web/app/tournaments.js`, `stopBtn` click handler.

---

## Bug 2 — Live-game window board grows unbounded

**Symptom:** The WinBox window attached to a tournament engine shows a board that can grow past the window boundary. Human-vs-engine doesn't have this problem because its WinBox has fixed pixel dimensions.

**Analysis:** `tournament-live-game.js` opens WinBox at `width:"30%"` / `height:"55%"` and calls `board.forceResize()` once. GameView's `ResizeObserver`-based sizing (computing `--board-max-px` from available width/height) is correct in principle, but the board column has no explicit `max-height`, so the observer fires with an unbounded available height.

**Recommended fix:** Add `max-height: 100%` (or equivalent) to the board column inside GameView's own CSS so the resize logic is self-limiting regardless of host window. Constraints belong in GameView, not in each host.

---

## Bug 3 — GameView shows only one engine's PV

**Symptom:** In the tournament live-game window only one PV line appears, even though two engines are playing.

**Analysis:** GameView has a single `enginePv` element (designed for human-vs-engine where one engine opposes the human). The proxy WebSocket (`/ws/tournament/proxy/{id}`) is per-engine, so a single live-game window only receives one engine's stream. Showing both PVs would require subscribing to both proxies simultaneously.

**Status:** Current behavior is consistent with single-proxy subscription. The user can open a second window via the second "attach" button to see the other engine's PV.

**Recommended fix (if desired):** Either (a) open both proxy subscriptions inside one window and render two PV rows, or (b) leave as-is and document the two-window attach pattern. Option (a) is a medium-sized architectural change.

---

## Bug 4 — Board not oriented from attached engine's perspective

**Symptom:** When attaching to engine 1 or engine 2 in a running tournament game, the board always shows white at the bottom regardless of which color that engine plays.

**Analysis:** `openLiveGameWindow` receives only `proxyId` and `label` — no color/side metadata. The `game_paired` event sends `{proxies: [pA, pB]}` in insertion order, not white-first. `pair_index.py` on the server does not track which proxy plays white.

**Recommended fix (client-side heuristic):** On the first `engine_info` / `go` event received from the proxy stream, the engine is thinking for the color whose turn it is in the current FEN. Read `board.turn` from the position at that moment and call `board.setOrientation()` accordingly. This requires no server changes.

**Alternative (server-side):** Extend `game_paired` to include `{white_proxy, black_proxy}` by parsing the first `position` command to determine which proxy moves first (white always moves first at ply 1).
