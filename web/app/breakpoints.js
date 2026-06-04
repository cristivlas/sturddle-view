// Named breakpoints, sourced from CSS custom properties on :root so the
// value lives in one place. Returns a MediaQueryList; consumers may read
// .matches or subscribe via addEventListener("change", ...).

const root = document.documentElement;

function read(name, fallback) {
  const v = getComputedStyle(root).getPropertyValue(name).trim();
  return v || fallback;
}

export const BP = {
  narrowDialog: read("--bp-narrow-dialog", "30rem"),
  mobile:       read("--bp-mobile",        "50rem"),
  mobileH:      read("--bp-mobile-h",      "30rem"),
  mobileHPlay:  read("--bp-mobile-h-play", "42.5rem"),
  wide:         read("--bp-wide",          "93.75rem"),
};

export const mqNarrowDialog = matchMedia(`(max-width: ${BP.narrowDialog})`);
export const mqMobile       = matchMedia(`(max-width: ${BP.mobile})`);
export const mqMobileH      = matchMedia(`(max-height: ${BP.mobileH})`);
export const mqMobileHPlay  = matchMedia(`(max-height: ${BP.mobileHPlay})`);
export const mqWide         = matchMedia(`(min-width: ${BP.wide})`);

// Drift guard: styles.css @media rules hardcode these breakpoints in px (no
// custom props in @media), so the rem var and the literal can diverge. Divisor
// is 16 -- @media resolves against the initial root px, not the html clamp.
const MEDIA_QUERY_ROOT_PX = 16;
const _MEDIA_LITERALS_PX = [
  ["--bp-narrow-dialog", 480],
  ["--bp-mobile", 800],
  ["--bp-mobile-h", 480],
  ["--bp-mobile-h-play", 680],
  ["--bp-wide", 1500],
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

_checkBreakpointDrift();
