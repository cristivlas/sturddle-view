// Shared duration formatting.

export const MS_PER_SECOND = 1000;
const SECONDS_PER_MINUTE = 60;
const SECONDS_PER_HOUR = 3600;
const SECONDS_PAD = 2;
const SECONDS_PER_DAY = 86400;
// Most units a worded duration shows.
const DURATION_PARTS = 3;
// Worded-duration units, finest last.
const DURATION_UNITS = Object.freeze([
  ["d", SECONDS_PER_DAY], ["h", SECONDS_PER_HOUR], ["m", SECONDS_PER_MINUTE], ["s", 1],
]);

// Elapsed text: "0s".."59s", "m:ss", "h:mm:ss" (hours unbounded). Floors,
// so a live timer ticks from 0s.
export function formatElapsed(ms) {
  const total = Math.max(0, Math.floor(ms / MS_PER_SECOND));
  if (total < SECONDS_PER_MINUTE) return `${total}s`;
  const pad = n => String(n).padStart(SECONDS_PAD, "0");
  const h = Math.floor(total / SECONDS_PER_HOUR);
  const m = Math.floor((total % SECONDS_PER_HOUR) / SECONDS_PER_MINUTE);
  const s = total % SECONDS_PER_MINUTE;
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

// Worded duration from the largest nonzero unit down: "2d 3h 14m", "3h 14m 5s",
// "14m 5s", "45s".
export function formatDuration(ms) {
  let rest = Math.max(0, Math.floor(ms / MS_PER_SECOND));
  const first = DURATION_UNITS.findIndex(([, sec]) => rest >= sec);
  const units = first < 0 ? DURATION_UNITS.slice(-1) : DURATION_UNITS.slice(first, first + DURATION_PARTS);
  return units.map(([label, sec]) => {
    const n = Math.floor(rest / sec);
    rest %= sec;
    return `${n}${label}`;
  }).join(" ");
}
