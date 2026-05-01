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

## Bug 3 — GameView shows only one engine's PV (deferred)

**Symptom:** In the tournament live-game window only one PV line appears, even though two engines are playing.

**Status:** Deferred to v1.5 / v2.0. Single-side observation is the supported model: each Schedule row is one active proxy (engine process); the user opens a second window for the opponent's POV.

**What we tried (and rolled back):** dual-WS window driven by automatic pair detection from UCI `position` lines. The pair_index could not handle `-concurrency > 1` reliably — parallel games can share opening-book prefixes, and any heuristic that disambiguates them either flaps (strict ply check + batched updates) or cross-pairs (prefix match across same-engine processes from different slots). Both modes were observed in practice.

**Path forward:** the deterministic option is a vendored fastchess fork that emits an `extended UCI` announcement on each game-start (`sturddle game-start slot=N white=X black=Y`); the proxy intercepts and forwards it to the orchestrator. With authoritative pairings, the dual-PV window comes back trivially. Until then we ship the simple single-side experience.

**Concurrency-related bugfix shipped now:** the proxy now generates its own `proxy_id` at startup (per-process uuid). Previously the id was baked into argv by `fastchess.py`, and `-concurrency > 1` caused multiple slot processes to share one id — which silently broke even single-side observation when concurrency > 1.

**Live-window snapshot fix shipped now:** the orchestrator keeps a per-proxy snapshot of the latest `position` / `go` / `info` lines. When a WS subscriber connects mid-game, the snapshot is replayed onto its queue so the window renders the engine's current state immediately instead of waiting for the next event (which under long time controls is often ≥10s away).

**Proxy stdio-loop fix shipped now:** the proxy's HTTP POSTs run in a background worker thread instead of blocking the asyncio loop. The previous synchronous `urllib.request.urlopen` call from inside the loop stalled the engine stdio pump under load and produced multi-second observable lag, especially with concurrency > 1.

---

## Bug 4 — Board not oriented from attached engine's perspective ✅ fixed

**Symptom:** When attaching to engine 1 or engine 2 in a running tournament game, the board always shows white at the bottom regardless of which color that engine plays.

**Analysis:** `openLiveGameWindow` received only `proxyId` and `label` — no color/side metadata.

**Fix:** On the first `go` event from the proxy stream, the engine is thinking for the color whose turn it is. The FEN from the preceding `position` event carries that turn in field 2. Read it and call `board.setSide()` accordingly — no server changes needed. File: `tournament-live-game.js`.
