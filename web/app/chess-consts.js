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

// PGN result tags (the "Result" header / game_result payload).
export const RESULT = {
  WHITE_WIN: "1-0",
  BLACK_WIN: "0-1",
  DRAW: "1/2-1/2",
};
