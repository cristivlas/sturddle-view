# Pause/Info button click-drop investigation

Status: **unresolved, handed off**. Reproducible but intermittent. A
diagnostic `console.log` is currently in the code (see "Current
instrumentation" below).

## Symptom

In the Tournaments perspective ribbon, the **Pause** and **Info**
buttons sometimes "don't take" when clicked. Specifically:

- Click visibly registers as a press (button hover/focus state) but
  **nothing happens** -- no spinner on Pause, no dialog from Info.
- Symptom is **intermittent** -- same button, same state, sometimes
  works, sometimes doesn't.
- **Spamming the button eventually works**: after several rapid
  clicks, one of them goes through.
- When Info eventually does open, the dialog **shows correct data**
  (server responds fine, just slowly under load in some cases).
- For Pause specifically, when broken: **the POST /stop request never
  reaches the server** (server log silent -- not even a request
  acknowledgement). This rules out server-side slowness for the Pause
  path specifically.

## What we ruled out

1. **WinBox workspace windows overlaying the ribbon** -- `openWorkspace()`
   computes `left = max(menubar.left, ribbonRect.right)` and passes it to
   `openTournamentWorkspace()`, which forwards it to every `new WinBox({left})`.
   WinBox clamps drag to `a.left` in its mousemove handler, so windows cannot
   be dragged left of the ribbon. Ruled out 2026-05-10.

3. **Button disabled state** -- user confirmed the button is not
   disabled when they click. Early-return at
   `web/app/tournaments.js:400` (`if (...ribbonStopBtn.disabled || stoppingId) return`)
   is not the cause.

4. **Stale `stoppingId` guard** -- the early-return on `stoppingId`
   only matters if the handler runs. See item 5.

5. **Ribbon button replaced/re-rendered** -- the ribbon `<button>`
   elements are created once at mount
   (`web/app/tournaments.js:18-78`, captured at lines 100-106) and
   never replaced. `syncRibbon()` only toggles `button.disabled` and
   swaps `button.innerHTML` between `<wa-icon>` and `<wa-spinner>` --
   the button itself stays, so its click listener is intact.

6. **Click handler firing-but-early-returning** -- a `console.log` at
   the very top of the handlers (currently in the code) confirms:
   when the bug is observed, **the log does NOT fire**. So the click
   is not reaching the JavaScript handler at all. This is the most
   important fact in this whole investigation.

7. **Heavy synchronous DOM rebuilds in event subscribers** -- we did
   land an rAF-coalesce for the event log render (commit
   `6b8cd7c`), but the bug persists after that fix. `renderSchedule`
   and `renderEngines` also rebuild via `innerHTML` per event but
   operate on small data (a few entries) -- likely not the cause but
   not proven.

8. **Server-side bottleneck** -- ruled out for Pause specifically
   because the POST never reaches the server. Possibly contributes to
   the *delay* of Info opening (when it eventually does), but is not
   the click-drop root cause.

## What this leaves

The click is not reaching the handler. The button is not disabled.
The element is in the DOM and visible. That narrows it to:

- **CSS `pointer-events: none`** on the button or an ancestor
  (transient, possibly during an animation/transition).
- **An invisible overlay** absorbing the click (a stale dialog host,
  modal backdrop, popover, or web-component shadow-DOM element with
  high z-index).
- **Web component shadow-DOM event interception** -- the `<wa-icon>`
  inside the button has a shadow root; if pointer events get stopped
  there it never bubbles to the button.
- **Main thread starvation** -- so unresponsive that the browser's
  input event queue stalls. Less likely given that hover state still
  works on the same button.
- **mousedown/mouseup landing on different elements** -- a focus or
  layout shift between press and release.

These are all consistent with the symptoms (intermittent, no log
fires, no visible disabled state). None has been confirmed.

## Current instrumentation (in the code, on branch
`pgn-stats-hardening`, uncommitted)

`web/app/tournaments.js` -- diagnostic `console.log` at the top of
the Pause and Info click handlers:

```js
ribbonStopBtn.addEventListener("click", async () => {
  console.log("[diag] stop clicked", { t: selectedTournament()?.id, disabled: ribbonStopBtn.disabled, stoppingId });
  ...
});
ribbonInfoBtn.addEventListener("click", () => {
  console.log("[diag] info clicked", { t: selectedTournament()?.id });
  ...
});
```

User confirmed: when the bug repros, **no `[diag]` log appears**.
This is the load-bearing data point. Do not lose it.

Revert these lines once the root cause is found.

## Diagnostic script available

`scripts/diag_pause_stall.py` -- Playwright script that instruments
`window.fetch` and probes ribbon button state every 2s. Intended
for repro against a running server (`python scripts/diag_pause_stall.py`).
Was written for a related earlier hypothesis (hung fetches wedging
the guards); not yet adapted for the "click never reaches handler"
hypothesis.

A useful extension would be to add a `pointerdown`/`pointerup`/
`click` listener at the document level (capture phase) and log
which element actually received the event when buttons "don't take".
That would distinguish overlay-absorbing-click from
pointer-events-none from main-thread-stall.

## File reference

- Click handlers: `web/app/tournaments.js:398-412`
- `syncRibbon` (button enable/disable + icon/spinner swap):
  `web/app/tournaments.js` around line 320-370
- Ribbon HTML (created once at mount): `web/app/tournaments.js:18-78`
- Button references captured at mount: `web/app/tournaments.js:100-106`
- `guard()` pattern (for Info button):
  `web/app/tournaments.js:123-130, 389, 409-412`
- Event log render (rAF-coalesced, commit `6b8cd7c`):
  `web/app/tournament-workspace.js:508-571, 700`
- `renderSchedule` / `renderEngines` (not coalesced, suspect-not-cause):
  `web/app/tournament-workspace.js:420-505, 714-721`
- WS event bus: `web/app/main.js:33-50`
- WS client: `web/app/ws.js:13-33`
- `api()` fetch wrapper (no timeout): `web/app/main.js:15-29`

## Reproducer

1. Build a tournament with a fast time control and >= 2 engines.
2. Start it; let it run long enough that several games complete
   (30-60s observed).
3. With the workspace open and live events flowing, try clicking the
   ribbon Pause or Info button on the running tournament's row.
4. Some clicks will do nothing. Open devtools console; the `[diag]`
   log will not print on the "lost" click.

No deterministic recipe -- relies on event flux and timing. Worst
under heavy event load (many parallel games at fast TC).

## What to try next (handoff suggestions)

1. **Document-level capture listener** to see which element actually
   received the click when the button "didn't take":

   ```js
   document.addEventListener("click", (e) => {
     console.log("[diag-doc] click", e.target, "path:", e.composedPath().slice(0,5));
   }, true);
   ```

   If the path shows a non-button element on the broken clicks,
   that's the overlay/pointer-events answer. If the path shows the
   button but the button's own listener didn't fire, the listener was
   somehow detached.

2. **Pointer event sequence** -- add `pointerdown`, `pointerup`,
   `click` listeners and log timestamps. If `pointerdown` fires but
   `click` doesn't, focus/layout moved between press and release.

3. **Performance recording** during a known-bad click. Look for:
   - Long tasks blocking the main thread around click time
   - Layout/paint storms from event subscribers
   - Forced reflow

4. **Web Awesome version check** -- if `<wa-icon>`/`<wa-button>`
   internals stop propagation in shadow DOM, that's an upstream bug
   we'd want pinned.

5. **Check `pointer-events` CSS** on the button, ribbon, container,
   and any sibling/ancestor that has a transition or animation.

## Related committed work on this branch

Don't undo these:

- `14f2620` -- bounded PGN tail polls; fixed an unrelated server-side
  Stop-doesn't-respond bug (the "Pause doesn't take" *server-side*
  variant, which is **different** from this UI click-drop).
- `a0b548d` -- warn-once for oversized PGN games.
- `68de9a6` -- kill proc if `start()` fails post-spawn.
- `eefb158` -- TODO note for POSIX SIGTERM grace race.
- `6b8cd7c` -- rAF-coalesce event log render. Real perf win, did NOT
  fix the click-drop bug.
- `ed8a698`, `aebb032` -- audit doc updates.

## Backup branch

`backup/restart-attempt` -- a rejected approach (process-supervisor
+ restart on Stop hang). Don't revive without rethinking; the user
explicitly rejected the UX.
