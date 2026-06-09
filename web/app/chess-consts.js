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
