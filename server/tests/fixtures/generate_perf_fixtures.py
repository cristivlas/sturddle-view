"""Deterministic generator for perf fixture PGN files.

Run once to (re)generate:
    python -m tests.fixtures.generate_perf_fixtures

Output files (relative to server/):
    tests/fixtures/perf_1k_games.pgn   -- 1000 short games
    tests/fixtures/perf_100_ply_game.pgn -- one 100-ply game
"""
from __future__ import annotations

import pathlib
import random

import chess
import chess.pgn

FIXTURES = pathlib.Path(__file__).parent
SEED = 42
SHORT_GAME_PLIES = 20
LONG_GAME_PLIES = 100
NUM_GAMES = 1000


def _make_game(rng: random.Random, plies: int, index: int) -> chess.pgn.Game:
    board = chess.Board()
    game = chess.pgn.Game()
    game.headers["Event"] = "PerfFixture"
    game.headers["White"] = "EngineA"
    game.headers["Black"] = "EngineB"
    game.headers["Round"] = str(index + 1)
    game.headers["Date"] = "2024.01.01"

    node = game
    for _ in range(plies):
        moves = list(board.legal_moves)
        if not moves:
            break
        move = rng.choice(moves)
        node = node.add_variation(move)
        board.push(move)
        if board.is_game_over():
            break

    outcome = board.outcome()
    game.headers["Result"] = outcome.result() if outcome is not None else "*"
    return game


def _write_pgn(path: pathlib.Path, games: list[chess.pgn.Game]) -> None:
    with path.open("w", encoding="ascii") as fh:
        for game in games:
            print(game, file=fh, end="\n\n")


def _generate(seed: int, plies: int, count: int) -> list[chess.pgn.Game]:
    rng = random.Random(seed)
    return [_make_game(rng, plies, i) for i in range(count)]


def main() -> None:
    out_1k = FIXTURES / "perf_1k_games.pgn"
    print(f"Generating {NUM_GAMES} short games -> {out_1k}")
    _write_pgn(out_1k, _generate(SEED, SHORT_GAME_PLIES, NUM_GAMES))

    out_long = FIXTURES / "perf_100_ply_game.pgn"
    print(f"Generating 100-ply game -> {out_long}")
    _write_pgn(out_long, _generate(SEED + 1, LONG_GAME_PLIES, 1))

    print("Done.")


if __name__ == "__main__":
    main()
