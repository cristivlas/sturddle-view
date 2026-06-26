// Shared adjudication/type formatters for the tournament info dialog (verbose
// dt/dd rows) and the Studio fight-bill wall (terse fine print). Same facts,
// two registers, selected by `verbose`. Resign/draw return null when unset;
// the dialog supplies its own "Off" sentinel so its row still renders.

const TYPE_LABEL = Object.freeze({ roundrobin: "Round-robin", gauntlet: "Gauntlet" });

export function formatType(v) {
  if (!v) return null;
  return TYPE_LABEL[v] || (v.charAt(0).toUpperCase() + v.slice(1));
}

export function formatResign(r, { verbose = false } = {}) {
  if (!r || r.movecount == null || r.score == null) return null;
  const base = verbose
    ? `after ${r.movecount} moves at ±${r.score} cp`
    : `${r.movecount} moves @ ${r.score}cp`;
  return r.twosided ? `${base} (two-sided)` : base;
}

export function formatDraw(d, { verbose = false } = {}) {
  if (!d || d.movenumber == null) return null;
  return verbose
    ? `from move ${d.movenumber}, ${d.movecount} moves within ±${d.score} cp`
    : `move ${d.movenumber}, ${d.movecount} @ ${d.score}cp`;
}
