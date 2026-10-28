// Chess primitive string constants shared producer->consumer.
// Cross-ref: server/sturddle_view/chess/results.py (SIDE_*).

// Side-to-move wire strings, carried in the board_update/clock_tick
// `turn` field and other STM payloads.
export const SIDE = {
  WHITE: "white",
  BLACK: "black",
};

// FEN side-to-move field (second token); also the in-edit STM token.
export const FEN_STM = {
  WHITE: "w",
  BLACK: "b",
};
// Space-separated FEN field indexes.
export const FEN_FIELD = {
  STM: 1,
  FULLMOVE: 5,
};

// SIDE.* for a FEN side-to-move token.
export function stmSide(stm) {
  return stm === FEN_STM.BLACK ? SIDE.BLACK : SIDE.WHITE;
}

export function fenSide(fen) {
  return stmSide(fen.split(" ")[FEN_FIELD.STM]);
}

// PGN result tags (the "Result" header / game_result payload).
export const RESULT = {
  WHITE_WIN: "1-0",
  BLACK_WIN: "0-1",
  DRAW: "1/2-1/2",
};

// MultiPV rank of the best line; engine_info without `multipv` is single-PV.
export const BEST_RANK = 1;

// UCI long-algebraic move: from-square, to-square, optional promotion piece.
const SQUARE_LEN = 2;

// [from, to] squares of a UCI move, or null when too short to be one.
export function uciSquares(uci) {
  if (!uci || uci.length < 2 * SQUARE_LEN) return null;
  return [uci.slice(0, SQUARE_LEN), uci.slice(SQUARE_LEN, 2 * SQUARE_LEN)];
}
