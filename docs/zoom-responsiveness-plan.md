# Zoom and Responsiveness Fix Plan

Date: 2026-05-19
Status: Proposal (no code changes)
Companion: [zoom-responsiveness-audit.md](./zoom-responsiveness-audit.md)

## Goal

Make the app honor browser zoom (Ctrl+/-) and user font-size preferences by replacing JS-driven px layout with CSS-driven layout. No visual changes at the default zoom level.

## Constraints

- cm-chessboard requires an explicit `boardEl.clientWidth` to render SVG squares; it cannot be made fully CSS-driven. The board element itself must have a measurable px width, but the *container* that sizes it should be CSS-driven.
- WinBox windows (debug docks, tournament live game) accept px-only positioning APIs.
- Existing tests cover behavior not pixel values; layout regressions need visual verification.

## Approach overview

1. Replace JS pixel breakpoints with CSS media queries and container queries.
2. Replace JS px sizing with CSS `aspect-ratio`, `grid-template-columns`, `clamp()`, and `min/max` constraints.
3. Keep JS measurement only where cm-chessboard needs it -- but reduce to a single read-write step driven by a CSS-sized parent.
4. Promote magic numbers (480/640/1500) to a single `--breakpoint-*` set of CSS custom properties.

## File-by-file plan

### 1. web/app/game-view.js + styles.css (highest impact)

**Current**: `_recomputeNow()` reads 5+ rects per frame, branches on `innerWidth` against 640/1500, writes board/rail/host sizes in px, sets 3 CSS custom properties from JS.

**Proposal**:

- Define CSS layout in `styles.css`:
  - `.play-grid { display: grid; grid-template-columns: var(--left-rail-w) minmax(320px, 1fr) var(--right-rail-w); gap: var(--grid-gap); }`
  - Use `@media (max-width: 40rem)` (= 640px at default font) to collapse to single column.
  - Use `@media (min-width: 93.75rem)` (= 1500px) for wide layout rail clamping.
  - Board column: `.game-view-board { aspect-ratio: 1; max-width: 100%; max-height: calc(100dvh - var(--board-vertical-reserve)); margin-inline: auto; }` where `--board-vertical-reserve` is the sum of clocks + controls heights expressed via flex on the column.
- Reduce JS to a single responsibility: measure the CSS-sized board column, set `boardEl.style.width` to match (cm-chessboard requirement), call `board.forceResize()`. No rail sizing, no host positioning, no breakpoint branching.
- Remove `--board-col-px`, `--left-rail-w` writes from JS; let grid columns be CSS-driven.
- Remove `sideHost.style.left/top/width` absolute positioning; place side rail as a grid child instead.

**Trade-off**: cm-chessboard's SVG-driven sizing creates a feedback loop with parent `aspect-ratio`. Mitigation: use `contain: size layout` on the board column, or wrap board in a `position: relative` parent of fixed aspect with the board absolutely positioned to fill it. Needs prototyping before commitment.

**Lines removed**: ~70 (lines 308-433 collapse to ~30).

### 2. web/app/play-debug-windows.js

**Current**: `MOBILE_MAX_W_PX=640` gates layout. `applyDockBounds()` reads board rect, writes dock width/top/bottom in px.

**Proposal**:

- Replace `isMobileLayout()` body with `matchMedia("(max-width: 40rem)").matches` (resolves to `window.matchMedia` once per call; could be cached and updated via the `change` event).
- Dock positioning: WinBox API requires px. Keep the measure-write, but treat it as positional (where to anchor the window), not layout (don't size the dock content with px). Acceptable per design rules -- WinBox windows are floating overlays, not flow containers.
- Move the 640px literal to a shared constant (e.g. `--bp-mobile: 40rem` in CSS, read via `getComputedStyle(document.documentElement).getPropertyValue('--bp-mobile')` once at init).

### 3. web/app/engines.js -- SKIPPED

**Status**: Skipped 2026-05-19 after structural review.

**Rationale**: The dialog body chain is `wa-dialog::part(body)` -> `wa-tab-group` -> `wa-tab-panel::part(base)` -> host div -> `engines-list-host`. Web Awesome's `wa-tab-panel` shadow DOM does not propagate flex through `::part(base)` -- the existing inline comment in [styles.css:1782-1786](../web/styles.css#L1782-L1786) acknowledges this is why `sizeWrap()` exists in the first place ("keeps the dialog body chain untouched -- no overflow hacks, no height inheritance through wa-tab-panel slot wrappers").

A CSS-only fix would require either:
- Forcing `display: flex; min-height: 0` on multiple `::part()` selectors and hoping wa-tab-panel's internal layout cooperates (high regression risk for a low-impact panel).
- Replacing `wa-tab-panel` with a custom tab implementation (out of scope).

`sizeWrap()` is a single-pass measure-write (not a feedback loop) and runs only on dialog open / resize / tab-show. The remaining wins from converting its `8`/`120` px constants to rem are marginal. Defer until a broader Web Awesome shadow-DOM workaround pattern is established or wa-tab-panel itself is replaced.

### 4. web/app/tournament-live-game.js

**Current**: `constrainAndResize()` measures `body.clientHeight/clientWidth`, computes a square `sz`, writes `boardHost`, `clockTop`, `clockBottom` widths in px. WinBox window placement uses raw `innerWidth/innerHeight`.

**Proposal**:

- Body layout: `.live-game-body { display: grid; grid-template-rows: auto 1fr auto; }` (top clock, board, bottom clock). Board row: `min-height: 0`.
- Board host: `aspect-ratio: 1; max-width: 100%; max-height: 100%; margin-inline: auto`. cm-chessboard still needs its px width: set `boardEl.style.width = boardHost.clientWidth + "px"` once per resize (single read-write, not a layout decision).
- Clocks: `width: var(--live-board-w, 100%)` where `--live-board-w` is set from JS once after board sizing -- or `width: min-content` if the clock content naturally matches.
- WinBox placement: leave as-is. Floating windows are exempt per the "px for overlays" carve-out.

### 5. web/styles.css mixed-unit calcs

**Current**:

- Line 1852: `max-height: calc(100vh - 240px)` (tournaments list)
- Line 1760: `max-height: calc(var(--dialog-height, 80vh) - 64px)` (dialog body)

**Proposal**:

- Line 1852: replace with `max-height: calc(100dvh - 15rem)` (15rem = 240px at default font, scales with zoom). Or restructure the dialog to use flex with `min-height: 0` and drop the explicit max-height.
- Line 1760: replace `64px` with `4rem`. Or remove the calc and let the body flex naturally inside the dialog frame.

### 6. Tier-2 -- 480px duplication

**Current**: Literal `(max-width: 480px)` appears in three JS files via `matchMedia()`.

**Proposal**:

- Add `--bp-narrow-dialog: 30rem` to `:root` in `styles.css` (30rem = 480px).
- In JS, read once at module load: `const BP_NARROW = getComputedStyle(document.documentElement).getPropertyValue('--bp-narrow-dialog').trim()`. Use `matchMedia(\`(max-width: ${BP_NARROW})\`)`.
- Optional follow-up: factor a tiny `web/app/breakpoints.js` exporting named MediaQueryList objects.

### 7. Tier-2 -- pervasive px spacing in styles.css

**Current**: 80+ `padding/margin/gap` in px.

**Proposal**:

- Out of scope for a focused zoom fix -- low impact (small values, browser zoom scales them via root font-size if set in em, but px don't follow `font-size` setting changes).
- If pursued: bulk replace by category (`gap: 8px` -> `gap: 0.5rem`, `padding: 4px 12px` -> `padding: 0.25rem 0.75rem`). Mechanical edit, but visual regression risk is non-trivial -- each section needs eyeball verification.
- Recommendation: defer to a separate pass with its own PR.

## Sequencing and status

1. **Phase 1** -- DONE 2026-05-19. 480px dedup (added `--bp-narrow-dialog`/`--bp-mobile`/`--bp-wide` to `:root`, created `web/app/breakpoints.js`), styles.css:1760 `64px`->`4rem`, styles.css:1852 `100vh - 240px`->`100dvh - 15rem`. Validated at 100% zoom.
2. **Phase 2** -- SKIPPED. See [section 3](#3-webappenginesjs----skipped) above. Web Awesome `wa-tab-panel` shadow DOM blocks flex chain; risk outweighs benefit.
3. **Phase 3** -- DONE 2026-05-19. tournament-live-game.js: replaced JS px sizing of board+clocks with CSS aspect-ratio + flex; JS publishes `--lg-board-w` for clock clamp. Reserved eval/pv row heights (1.4em) to eliminate board snap-shrink on first event.
4. **Phase 4** -- DONE 2026-05-19 (reduced scope). The ambitious form (delete `_recomputeNow` entirely, replace with CSS grid + aspect-ratio) was prototyped and reverted: the chicken-and-egg between board-square width and grid column width broke under zoom/resize. Landed instead: (a) `html { font-size: clamp(14px, 1em, 24px) }` so layout stays usable across the full chrome://settings/fonts range; (b) MIN_BOARD/RAIL_MIN/RAIL_MAX and floor minimums in `_recomputeNow` are now rem-derived; NARROW=640 and WIDE=1500 stay raw CSS-px (they're viewport thresholds matching CSS media queries, not size scales). The `_recomputeNow` measure-write cycle itself remains.
5. **Phase 5** -- DONE 2026-05-19. play-debug-windows.js `isMobileLayout()` now reads `mqMobile` from breakpoints.js; removed `MOBILE_MAX_W_PX` export.
6. **Phase 6** (optional) -- DEFERRED. styles.css px-to-rem spacing pass. Lower priority now that root font-size is clamped.

## Verification

- Default zoom (100%): visual diff against current state at 1920x1080, 1280x720, 768x1024, 375x812.
- Zoom 150% and 200%: board remains square, side rail still visible at >=1280, clocks remain aligned with board width, dialogs remain within viewport.
- Browser font-size 20px (default 16): rem-based sizing scales; px-based hairlines do not (correct).
- No new JS errors during resize or zoom.
- Existing test suite passes (no test is expected to depend on the px values being audited).

## Risks

- cm-chessboard's SVG sizing reacts to its container; CSS-driven aspect-ratio may produce subtle race conditions on first paint. Mitigation: keep one explicit `forceResize()` call after CSS settles.
- Web Awesome dialog shadow DOM may resist flex restructuring. Mitigation: test on the engines dialog first (Phase 2) before relying on the pattern elsewhere.
- Grid-based side rail placement loses the current "stick to board's right edge with px offset" precision. Mitigation: this is what `grid-template-columns` with named tracks is for; visually equivalent without JS.

## Out of scope

- The 80+ px spacing values in `styles.css` (Tier 2, deferred).
- Dialog `min(Npx, Mvw)` sizing patterns (acceptable per audit).
- `vh` panel heights (capped, tolerable per audit).
- Any visual or behavioral change beyond zoom/responsiveness compliance.
