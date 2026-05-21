// Map a server termination code (snake_case enum name from python-chess
// or our own resignation/time_forfeit/unterminated tags) to a phrase fit
// for user-facing prose. Unknown codes fall back to underscores->spaces
// so we never surface "Time_forfeit" or similar raw enum text.

const TERMINATION_REASONS = {
  checkmate: "checkmate",
  stalemate: "stalemate",
  insufficient_material: "insufficient material",
  seventyfive_moves: "75-move rule",
  fivefold_repetition: "fivefold repetition",
  fifty_moves: "50-move rule",
  threefold_repetition: "threefold repetition",
  time_forfeit: "time forfeit",
  resignation: "resignation",
  variant_win: "variant win",
  variant_loss: "variant loss",
  variant_draw: "variant draw",
  unterminated: "unterminated",
  normal: "normal",
};

function _capitalize(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// Lowercase phrase, no leading capital. For interpolation into prose
// like "... ended by time forfeit." or display in a small label.
export function terminationPhrase(termination) {
  if (!termination) return "";
  return TERMINATION_REASONS[termination] ?? termination.replace(/_/g, " ");
}

// Sentence-leading form, e.g. "Time forfeit". For use at the head of a
// game-over alert: `${terminationLabel(t)} -- White wins.`
export function terminationLabel(termination) {
  return _capitalize(terminationPhrase(termination) || "Game over");
}
