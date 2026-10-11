// Named breakpoints, sourced from CSS custom properties on :root so the
// value lives in one place. Returns a MediaQueryList; consumers may read
// .matches or subscribe via addEventListener("change", ...).

const root = document.documentElement;

function read(name, fallback) {
  const v = getComputedStyle(root).getPropertyValue(name).trim();
  return v || fallback;
}

const BP_VAR = {
  narrowDialog:    "--bp-narrow-dialog",
  settingsTopTabs: "--bp-settings-top-tabs",
  mobile:          "--bp-mobile",
  mobileH:         "--bp-mobile-h",
  mobileHPlay:     "--bp-mobile-h-play",
  wide:            "--bp-wide",
};

export const BP = {
  narrowDialog: read(BP_VAR.narrowDialog, "30rem"),
  settingsTopTabs: read(BP_VAR.settingsTopTabs, "46rem"),
  mobile:       read(BP_VAR.mobile,        "50rem"),
  mobileH:      read(BP_VAR.mobileH,       "30rem"),
  mobileHPlay:  read(BP_VAR.mobileHPlay,   "42.5rem"),
  wide:         read(BP_VAR.wide,          "93.75rem"),
};

export const mqNarrowDialog = matchMedia(`(max-width: ${BP.narrowDialog})`);
export const mqSettingsTopTabs = matchMedia(`(max-width: ${BP.settingsTopTabs})`);
export const mqMobile       = matchMedia(`(max-width: ${BP.mobile})`);
export const mqMobileH      = matchMedia(`(max-height: ${BP.mobileH})`);
// Touch only: short-but-wide mouse viewports are laptops (1366x768), not phones.
// Source of truth; styles.css height-gate @media rules repeat it (drift-checked).
const HEIGHT_GATE_POINTER = "(pointer: coarse)";
export const mqMobileHPlay  = matchMedia(`(max-height: ${BP.mobileHPlay}) and ${HEIGHT_GATE_POINTER}`);
export const mqWide         = matchMedia(`(min-width: ${BP.wide})`);

// Drift guard: styles.css @media rules hardcode these breakpoints in px (no
// custom props in @media), so the rem var and the literal can diverge. Divisor
// is 16 -- @media resolves against the initial root px, not the html clamp.
const MEDIA_QUERY_ROOT_PX = 16;
const _MEDIA_LITERALS_PX = [
  [BP_VAR.narrowDialog, 480],
  [BP_VAR.mobile, 800],
  [BP_VAR.mobileH, 480],
  [BP_VAR.mobileHPlay, 680],
  [BP_VAR.wide, 1500],
];

function _checkBreakpointDrift() {
  for (const [name, literalPx] of _MEDIA_LITERALS_PX) {
    const rem = parseFloat(getComputedStyle(root).getPropertyValue(name));
    if (!Number.isFinite(rem)) continue;
    const computedPx = Math.round(rem * MEDIA_QUERY_ROOT_PX);
    if (computedPx !== literalPx) {
      console.error(
        `breakpoint drift: ${name} = ${rem}rem -> ${computedPx}px at 16px root, ` +
        `but styles.css @media hardcodes ${literalPx}px. Resync the two.`,
      );
    }
  }
}

// Whitespace-free compare: engines differ in how they serialize conditionText.
const _squash = (s) => s.replace(/\s+/g, "");

// The styles.css height-gate @media rules must carry the same pointer clause.
function _checkHeightGatePointerDrift() {
  const [, gatePx] = _MEDIA_LITERALS_PX.find(([name]) => name === BP_VAR.mobileHPlay);
  const heightClause = _squash(`max-height: ${gatePx}px`);
  const pointerClause = _squash(HEIGHT_GATE_POINTER);
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; }
    for (const rule of rules) {
      if (!(rule instanceof CSSMediaRule)) continue;
      const cond = rule.conditionText;
      const squashed = _squash(cond);
      if (squashed.includes(heightClause) && !squashed.includes(pointerClause)) {
        console.error(`breakpoint drift: @media ${cond} lacks ${HEIGHT_GATE_POINTER}. Resync with mqMobileHPlay.`);
      }
    }
  }
}

_checkBreakpointDrift();
_checkHeightGatePointerDrift();
