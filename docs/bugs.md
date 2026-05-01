# UI Bugs — Tournament Game View

Reported 2026-04-30.

---

## Bug 1 — Stop button not debounced ✅ fixed

**Symptom:** Clicking Stop sends the POST but leaves the button clickable/unstyled during the async wait.

**Fix:** Disable the button and swap its icon for `<wa-spinner>` immediately on click. `loadList()` re-renders the row so no manual restore is needed.

**File:** `web/app/tournaments.js`, `stopBtn` click handler.

---

## Bug 2 — Live-game window board grows unbounded ✅ fixed

**Symptom:** The WinBox window attached to a tournament engine shows a board that can grow past the window boundary. Human-vs-engine doesn't have this problem because its WinBox has fixed pixel dimensions.

**Analysis:** `tournament-live-game.js` opened WinBox at `width:"30%"` / `height:"55%"` and called `board.forceResize()` once. cm-chessboard sizes its SVG to the element's *width*, so if the window was wider than tall the SVG overflowed vertically. There was also no ResizeObserver, so WinBox resize never re-triggered sizing.

**Fix:** Added a `ResizeObserver` on the WinBox body that clamps `boardHost.style.width` to `clientHeight` whenever the window is wider than tall, then calls `forceResize()`. Added `overflow: hidden` to `.wb-livegame .lg-board` as a backstop. Files: `tournament-live-game.js`, `styles.css`.

---

## Bug 3 — GameView shows only one engine's PV

**Symptom:** In the tournament live-game window only one PV line appears, even though two engines are playing.

**Analysis:** The proxy WebSocket (`/ws/tournament/proxy/{id}`) is per-engine, so a single live-game window only receives one engine's stream. Showing both PVs would require subscribing to both proxies simultaneously.

**Status:** Open — proper fix is to open both proxy WebSocket subscriptions inside one live-game window and render two PV rows side-by-side (one per engine). This is a medium-sized change touching `tournament-live-game.js` layout and the `game_paired` event (which carries both proxy IDs). The workaround is opening a second attach window for the other engine.

---

## Bug 4 — Board not oriented from attached engine's perspective ✅ fixed

**Symptom:** When attaching to engine 1 or engine 2 in a running tournament game, the board always shows white at the bottom regardless of which color that engine plays.

**Analysis:** `openLiveGameWindow` received only `proxyId` and `label` — no color/side metadata.

**Fix:** On the first `go` event from the proxy stream, the engine is thinking for the color whose turn it is. The FEN from the preceding `position` event carries that turn in field 2. Read it and call `board.setSide()` accordingly — no server changes needed. File: `tournament-live-game.js`.
