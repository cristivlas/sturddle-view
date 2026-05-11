import chess
import chess.syzygy

MAX_PIECES = 6


class TablebaseProber:
    def __init__(self, path: str) -> None:
        self.path = path
        self._tb = chess.syzygy.open_tablebase(path)

    def probe(self, board: chess.Board) -> dict | None:
        if board.is_game_over():
            return None
        if sum(1 for _ in board.piece_map().values()) > MAX_PIECES:
            return None
        try:
            wdl = self._tb.probe_wdl(board)
            dtz = self._tb.probe_dtz(board)
        except (KeyError, chess.syzygy.MissingTableError):
            return None
        return {"wdl": wdl, "dtz": dtz}

    def close(self) -> None:
        self._tb.close()
