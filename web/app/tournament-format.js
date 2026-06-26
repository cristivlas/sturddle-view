// Shared adjudication/type formatters. Two registers: the verbose dialog rows
// (formatResign/formatDraw) and the terse Studio fight-bill wall (shortResign/
// shortDraw). All return null when unset; the dialog supplies its own "Off"
// sentinel so its row still renders.

const TYPE_LABEL = Object.freeze({ roundrobin: "Round-robin", gauntlet: "Gauntlet" });

export function formatType(v) {
  if (!v) return null;
  return TYPE_LABEL[v] || (v.charAt(0).toUpperCase() + v.slice(1));
}

// Signed Elo with a leading "+" on non-negatives (e.g. "+12.3", "-4.5").
export function fmtSignedElo(v) {
  return (v >= 0 ? "+" : "") + v.toFixed(1);
}

// " +/- m.m" margin suffix, or "" when no 95% interval is available.
export function fmtMargin(m) {
  return m == null ? "" : ` ± ${m.toFixed(1)}`;
}

export function formatResign(r) {
  if (!r || r.movecount == null || r.score == null) return null;
  const base = `after ${r.movecount} moves at ±${r.score} cp`;
  return r.twosided ? `${base} (two-sided)` : base;
}

export function formatDraw(d) {
  if (!d || d.movenumber == null) return null;
  return `from move ${d.movenumber}, ${d.movecount} moves within ±${d.score} cp`;
}

export function shortResign(r) {
  if (!r || r.movecount == null || r.score == null) return null;
  return `${r.movecount}@${r.score}cp`;
}

export function shortDraw(d) {
  if (!d || d.movenumber == null) return null;
  return `${d.movenumber},${d.movecount}@${d.score}cp`;
}
