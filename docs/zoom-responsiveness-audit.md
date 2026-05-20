# Zoom and Responsiveness Audit (web/)

Date: 2026-05-19
Scope: `web/` first-party CSS, HTML, and JS (excludes node_modules, vendored libs, minified files).

## Design rules under audit

- `rem` for text and spacing
- `%` / `flex` / `grid` for layout
- `px` OK for borders and tiny details (hairlines, icon sizes)
- Avoid fixed pixel widths for containers
- In JS, don't hardcode pixel breakpoints -- let CSS handle it
- Avoid layout-thrashing JS (forced reflow, manual resize-driven sizing)

## Tier 1 -- zoom-hostile, fix priority

### JS pixel breakpoints driving layout

- [web/app/game-view.js:339-342](../web/app/game-view.js#L339-L342) -- constants `NARROW=640`, `MIN_BOARD=320`, `RAIL_MIN=180`, `RAIL_MAX=320`
- [web/app/game-view.js:361](../web/app/game-view.js#L361) -- `window.innerWidth <= NARROW` gates rail layout
- [web/app/game-view.js:375](../web/app/game-view.js#L375) -- breakpoint-driven board width selection
- [web/app/game-view.js:395](../web/app/game-view.js#L395) -- `window.innerWidth > NARROW` sets/unsets CSS custom props
- [web/app/game-view.js:407](../web/app/game-view.js#L407) -- breakpoint gates side-host positioning
- [web/app/game-view.js:415-417](../web/app/game-view.js#L415-L417) -- `WIDE=1500` constant, drives rail width clamp
- [web/app/play-debug-windows.js:47](../web/app/play-debug-windows.js#L47) -- `MOBILE_MAX_W_PX=640`
- [web/app/play-debug-windows.js:50](../web/app/play-debug-windows.js#L50) -- `isMobileLayout()` based on `window.innerWidth`

### JS px-based sizing/positioning of containers

- [web/app/game-view.js:310](../web/app/game-view.js#L310) -- `boardEl.style.width = "0"` (forced reflow reset)
- [web/app/game-view.js:381](../web/app/game-view.js#L381) -- `boardEl.style.width = ${max}px`
- [web/app/game-view.js:384-385](../web/app/game-view.js#L384-L385) -- inner board width/height in px
- [web/app/game-view.js:390-397](../web/app/game-view.js#L390-L397) -- `--board-max-px`, `--board-col-px`, `--left-rail-w` written from JS in px
- [web/app/game-view.js:419-421](../web/app/game-view.js#L419-L421) -- `sideHost.style.left/top/width` set in px
- [web/app/game-view.js:422](../web/app/game-view.js#L422) -- `sideHost.style.max-height` set in px
- [web/app/play-debug-windows.js:60](../web/app/play-debug-windows.js#L60) -- dock width = `(boardLeft - ribbonW - 9)` px
- [web/app/play-debug-windows.js:67-69](../web/app/play-debug-windows.js#L67-L69) -- dock top/bottom in px, height cleared
- [web/app/engines.js:469](../web/app/engines.js#L469) -- `wrapEl.style.height = h + "px"`
- [web/app/engines.js:477](../web/app/engines.js#L477) -- same, after overflow correction
- [web/app/tournament-live-game.js:279-280](../web/app/tournament-live-game.js#L279-L280) -- board host width/height in px
- [web/app/tournament-live-game.js:282](../web/app/tournament-live-game.js#L282) -- clocks width in px

### Layout-thrashing read/write cycles

- [web/app/game-view.js:288,294,311,317,408](../web/app/game-view.js#L308) -- 5+ `getBoundingClientRect()` calls per `_recomputeNow()`, then px writes at 381/384/385/419-422; ResizeObserver on `boardCol` and `document.body` at 447-449 plus window resize listener at 450
- [web/app/engines.js:463-467](../web/app/engines.js#L463-L467) -- measures dialog body geometry, writes wrapper height in px
- [web/app/play-debug-windows.js:57-69](../web/app/play-debug-windows.js#L57-L69) -- measures board rect, writes dock geometry

### Mixed-unit `calc()` (breaks under zoom)

- [web/styles.css:1852](../web/styles.css#L1852) -- `max-height: calc(100vh - 240px)` (tournaments-list)
- [web/styles.css:1760](../web/styles.css#L1760) -- `max-height: calc(var(--dialog-height, 80vh) - 64px)` (dialog body, lower impact)

### Raw viewport reads for placement

- [web/app/tournament-live-game.js:106-107,117](../web/app/tournament-live-game.js#L106-L107) -- `window.innerWidth/innerHeight` for WinBox overlap-avoidance placement
- [web/app/tournament-live-game.js:198](../web/app/tournament-live-game.js#L198) -- `window.innerWidth * 0.20` (percent-based, less severe)

## Tier 2 -- minor violations

### Hardcoded 480px breakpoint duplicated in JS

- [web/app/engine-options-dialog.js:345](../web/app/engine-options-dialog.js#L345) -- `matchMedia("(max-width: 480px)")`
- [web/app/settings-dialog.js:123](../web/app/settings-dialog.js#L123) -- same literal
- [web/app/settings-dialog.js:146](../web/app/settings-dialog.js#L146) -- same literal

Uses `matchMedia` (correct mechanism) but repeats magic number.

### `vh` panel heights (capped, tolerable)

- [web/styles.css:1012](../web/styles.css#L1012) -- `min-height: 60vh`
- [web/styles.css:1019](../web/styles.css#L1019) -- `min-height: 40vh`
- [web/styles.css:1061](../web/styles.css#L1061) -- `--engine-settings-panel-h: 40vh`
- [web/styles.css:1637](../web/styles.css#L1637) -- `height: 50vh`
- [web/styles.css:1660](../web/styles.css#L1660) -- `min-height: 50vh`

### Dialog `min(Npx, Mvw)` sizing

- [web/app/dialogs.js:77](../web/app/dialogs.js#L77) -- `width: min(440px, 92vw)`
- [web/app/dialogs.js:189](../web/app/dialogs.js#L189) -- `width: min(800px, 90vw)`
- [web/app/settings-dialog.js:121](../web/app/settings-dialog.js#L121) -- `width: min(690px, 94vw)`
- [web/app/engine-options-dialog.js:329](../web/app/engine-options-dialog.js#L329) -- `width: min(560px, 94vw)`
- [web/app/import-position-dialog.js:57](../web/app/import-position-dialog.js#L57) -- `width: min(480px, 92vw)`
- [web/app/about-dialog.js:15](../web/app/about-dialog.js#L15) -- `width: min(300px, 90vw)`
- [web/app/tournaments.js:521,721](../web/app/tournaments.js#L521) -- `height: min(720px, 92vh)`, `width: min(720px, 94vw)`

Acceptable: `min()` caps absolute px under tight viewports.

### Pervasive px spacing in styles.css

80+ `padding`/`margin`/`gap` declarations using px values (mostly 4-16px). Examples around lines 108-109, 121, 142, 169, 231. Functionally fine; violates "rem for spacing" rule but low zoom impact.

## Tier 3 -- compliant / acceptable

- No fixed-px `width`/`min-width`/`max-width` on layout containers (no Tier-1 container violations found).
- Borders, icon sizes, drag overlays, column-resize visual feedback in px (per design rules).
- ResizeObserver loops, while present, do not by themselves violate rules.

## Root pattern

The dominant offender is [web/app/game-view.js](../web/app/game-view.js)'s `_recomputeNow()`: measures container with `getBoundingClientRect()`, branches on `window.innerWidth` against three hardcoded breakpoints (640/1500 + 320/180 minimums), then writes px sizes for the board, the rails, and four CSS custom properties. Replacing the measure-write loop with CSS `aspect-ratio`, `container queries`, and grid track sizing would eliminate most of Tier 1 simultaneously.

The secondary pattern is dialog-internal panels ([engines.js](../web/app/engines.js), [tournament-live-game.js](../web/app/tournament-live-game.js)) that measure parent geometry and write child px sizes. These exist because of cm-chessboard's SVG-sizing requirement (squares need an explicit pixel width) and dialog body overflow behavior. Both can be replaced with CSS `aspect-ratio: 1` + flex `min-height: 0` patterns.
