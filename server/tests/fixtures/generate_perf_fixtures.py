"""Deterministic generator for perf fixture PGN files.

Run once to (re)generate:
    python -m tests.fixtures.generate_perf_fixtures

Output files (relative to server/):
    tests/fixtures/perf_1k_games.pgn    -- 1000 real games sampled from games-data
    tests/fixtures/perf_200_games.pgn   -- first 200 games of the 1k set (perf bench subset)
    tests/fixtures/perf_100_ply_game.pgn -- one 100-ply synthetic game
"""
from __future__ import annotations

import pathlib
import random

import chess
import chess.pgn

FIXTURES = pathlib.Path(__file__).parent
SEED = 42
LONG_GAME_PLIES = 100
NUM_GAMES = 1000

# Real game sources relative to this file's grandparent (server/) or absolute.
_GAMES_DATA = pathlib.Path(r"C:\Users\crist\Projects\games-data")
_SOURCE_PGNS = [
    _GAMES_DATA / "self-01.pgn",
    _GAMES_DATA / "tour.pgn",
    _GAMES_DATA / "tour.SkylarkII.pgn",
    _GAMES_DATA / "kiwi.pgn",
    _GAMES_DATA / "tour-2.pgn",
    _GAMES_DATA / "tou3r.pgn",
    _GAMES_DATA / "The Great Kiwi Hope R1-6 (GBSelect2025.cgb).pgn",
    _GAMES_DATA / "The Great Kiwi Hope R7-12 (GBSelect2025.cgb).pgn",
    _GAMES_DATA / "The Great Kiwi Hope R13-18 (GBSelect2025.cgb).pgn",
    _GAMES_DATA / "The Great Kiwi Hope R19-24 (GBSelect2025.cgb).pgn",
]


def _load_games(paths: list[pathlib.Path]) -> list[chess.pgn.Game]:
    games = []
    for path in paths:
        if not path.exists():
            print(f"  skipping missing: {path}")
            continue
        with path.open(encoding="utf-8", errors="replace") as f:
            while True:
                g = chess.pgn.read_game(f)
                if g is None:
                    break
                if g.headers.get("Result", "*") != "*":
                    games.append(g)
    return games


def _make_synthetic_game(rng: random.Random, plies: int, index: int) -> chess.pgn.Game:
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
    game.headers["Result"] = outcome.result() if outcome is not None else "1/2-1/2"
    return game


def _write_pgn(path: pathlib.Path, games: list[chess.pgn.Game]) -> None:
    with path.open("w", encoding="ascii", errors="replace") as fh:
        for game in games:
            print(game, file=fh, end="\n\n")


def main() -> None:
    rng = random.Random(SEED)

    print("Loading real games from games-data...")
    pool = _load_games(_SOURCE_PGNS)
    print(f"  pool size: {len(pool)}")

    if len(pool) >= NUM_GAMES:
        sample = rng.sample(pool, NUM_GAMES)
    else:
        print(f"  pool < {NUM_GAMES}; using all + synthetic fill")
        fill = [_make_synthetic_game(rng, 200, i) for i in range(NUM_GAMES - len(pool))]
        sample = pool + fill
        rng.shuffle(sample)

    out_1k = FIXTURES / "perf_1k_games.pgn"
    print(f"Writing {len(sample)} games -> {out_1k}")
    _write_pgn(out_1k, sample)

    out_200 = FIXTURES / "perf_200_games.pgn"
    print(f"Writing first 200 games -> {out_200}")
    _write_pgn(out_200, sample[:200])

    out_long = FIXTURES / "perf_100_ply_game.pgn"
    print(f"Generating 100-ply synthetic game -> {out_long}")
    _write_pgn(out_long, [_make_synthetic_game(random.Random(SEED + 1), LONG_GAME_PLIES, 0)])

    size = out_1k.stat().st_size
    decisive = sum(
        1 for g in sample if g.headers.get("Result", "*") != "*"
    )
    print(f"  size: {size:,} bytes, decisive: {decisive}/{len(sample)}")
    if size > 5 * 1024 * 1024:
        print("  WARNING: fixture exceeds 5 MB plan invariant")
    print("Done.")


if __name__ == "__main__":
    main()
