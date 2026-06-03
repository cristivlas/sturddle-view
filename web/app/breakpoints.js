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
  mobile:       read("--bp-mobile",        "40rem"),
  mobileH:      read("--bp-mobile-h",      "30rem"),
  mobileHPlay:  read("--bp-mobile-h-play", "46.25rem"),
  wide:         read("--bp-wide",          "93.75rem"),
};

export const mqNarrowDialog = matchMedia(`(max-width: ${BP.narrowDialog})`);
export const mqMobile       = matchMedia(`(max-width: ${BP.mobile})`);
export const mqMobileH      = matchMedia(`(max-height: ${BP.mobileH})`);
export const mqMobileHPlay  = matchMedia(`(max-height: ${BP.mobileHPlay})`);
export const mqWide         = matchMedia(`(min-width: ${BP.wide})`);
