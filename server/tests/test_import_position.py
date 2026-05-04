"""Parser for FEN/PGN imports + the /game/import HTTP surface."""
from __future__ import annotations

from unittest.mock import AsyncMock

import chess
import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.import_position import (
    PositionImportError,
    parse_fen,
    parse_pgn,
)


# ---------- parse_fen ----------


def test_parse_fen_starting_position():
    p = parse_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert p.side_to_move == "white"
    assert p.ply == 0
    assert p.moves_uci == []
    assert "White to move" in p.summary


def test_parse_fen_midgame_black_to_move():
    # After 1.e4 only.
    p = parse_fen("rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1")
    assert p.side_to_move == "black"
    assert p.start_fen == p.final_fen


def test_parse_fen_strips_whitespace():
    p = parse_fen(
        "  \n rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1  \n"
    )
    assert p.side_to_move == "white"


def test_parse_fen_empty_rejected():
    with pytest.raises(PositionImportError):
        parse_fen("")
    with pytest.raises(PositionImportError):
        parse_fen("   \n  ")


def test_parse_fen_invalid_rejected():
    with pytest.raises(PositionImportError, match="invalid FEN"):
        parse_fen("not-a-fen")


def test_parse_fen_accepts_finished_position():
    # Fool's mate position: White just got mated. Finished games are valid
    # for view mode (post-mortem inspection).
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    p = parse_fen(fen)
    assert p.start_fen == fen


# ---------- parse_pgn ----------


def test_parse_pgn_basic_mainline():
    pgn = (
        '[Event "Test"]\n[White "Carlsen"]\n[Black "Nakamura"]\n\n'
        "1. e4 e5 2. Nf3 Nc6 *"
    )
    p = parse_pgn(pgn)
    assert p.moves_uci == ["e2e4", "e7e5", "g1f3", "b8c6"]
    assert p.side_to_move == "white"
    assert p.ply == 4
    # Standard startpos PGN → start_fen is None so opening lookup engages.
    assert p.start_fen is None
    assert "Carlsen" in p.summary and "Nakamura" in p.summary
    assert p.headers["White"] == "Carlsen"


def test_parse_pgn_no_headers_summary():
    p = parse_pgn("1. e4 e5 2. Nf3 *")
    assert p.moves_uci == ["e2e4", "e7e5", "g1f3"]
    assert p.side_to_move == "black"
    # When both players are unknown the summary falls back to side-only.
    assert "Black to move" in p.summary


def test_parse_pgn_with_starting_fen_header():
    # PGN encodes a position via [FEN] / [SetUp]. Replays from that.
    fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    pgn = f'[SetUp "1"]\n[FEN "{fen}"]\n\n3. Bb5 *'
    p = parse_pgn(pgn)
    assert p.start_fen == fen
    assert p.moves_uci == ["f1b5"]
    assert p.side_to_move == "black"


def test_parse_pgn_empty_rejected():
    with pytest.raises(PositionImportError):
        parse_pgn("")


def test_parse_pgn_accepts_finished_game():
    # Scholar's mate. Finished games are valid for view mode.
    pgn = "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0"
    p = parse_pgn(pgn)
    assert p.moves_uci[-1] == "h5f7"
    assert p.ply == 7


def test_parse_fen_startpos_returns_none_start_fen():
    # Startpos FEN → None, so the opening book identifies subsequent moves.
    p = parse_fen("rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1")
    assert p.start_fen is None


def test_parse_pgn_extracts_clk_annotations():
    # PGN with [%clk] on each ply → clock_history reconstructed,
    # final_*_time set to the most recent per-side clock.
    pgn = (
        "1. e4 { [%clk 0:04:55] } 1... c5 { [%clk 0:04:50] } "
        "2. Nf3 { [%clk 0:04:48] } *"
    )
    p = parse_pgn(pgn)
    assert p.clock_history is not None
    assert len(p.clock_history) == 3
    # Pre-move ply 0: nobody has moved → both None (consumer fills with TC).
    assert p.clock_history[0] == (None, None)
    # Pre-move ply 1: white played e4, black hasn't moved.
    assert p.clock_history[1] == (4 * 60 + 55, None)
    # Pre-move ply 2: both have one move on the clock.
    assert p.clock_history[2] == (4 * 60 + 55, 4 * 60 + 50)
    # After ply 2 (white's Nf3) — final clocks for both sides.
    assert p.final_white_time == 4 * 60 + 48
    assert p.final_black_time == 4 * 60 + 50


def test_parse_pgn_no_clk_annotations_returns_none():
    p = parse_pgn("1. e4 e5 2. Nf3 *")
    assert p.clock_history is None
    assert p.final_white_time is None
    assert p.final_black_time is None


def test_parse_pgn_with_fen_header_preserves_start_fen():
    # Non-startpos FEN header → start_fen is the header value (lookup suppressed).
    fen = "r1bqkbnr/pppp1ppp/2n5/4p3/4P3/5N2/PPPP1PPP/RNBQKB1R w KQkq - 2 3"
    p = parse_pgn(f'[SetUp "1"]\n[FEN "{fen}"]\n\n3. Bb5 *')
    assert p.start_fen == fen


def test_parse_pgn_skips_variations_takes_mainline():
    # A variation shouldn't end up in moves_uci.
    pgn = "1. e4 e5 (1... c5 2. Nf3) 2. Nf3 Nc6 *"
    p = parse_pgn(pgn)
    assert p.moves_uci == ["e2e4", "e7e5", "g1f3", "b8c6"]


# ---------- /game/import + /game/import/validate ----------


@pytest.fixture
def client(tmp_path):
    fake_engine = tmp_path / "engine"
    fake_engine.write_text("")
    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = fake_engine
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    with TestClient(app) as c:
        yield c, app, str(fake_engine)


def _patch_hve(app, engine_path: str) -> HumanVsEngine:
    """Pre-install an HVE with stubbed engine spawn so /game/import can
    `_get_hve` and call `new_game` without launching a real subprocess."""
    hve = HumanVsEngine(
        engine_path=engine_path,
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )

    class _StubEngine:
        def send_line(self, _line: str) -> None:
            pass
        async def quit(self) -> None:
            return None

    async def fake_ensure_engine():
        hve._engine = _StubEngine()
        return hve._engine

    hve._ensure_engine = fake_ensure_engine
    hve._engine_to_move = AsyncMock()
    app.state.hve = hve
    return hve


def test_validate_endpoint_fen_ok(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={
        "format": "fen",
        "text": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["side_to_move"] == "white"
    assert body["moves_uci"] == []
    assert "summary" in body


def test_validate_endpoint_bad_fen_returns_400(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={"format": "fen", "text": "garbage"})
    assert r.status_code == 400
    assert "invalid FEN" in r.json()["detail"]


def test_validate_endpoint_pgn_ok(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={
        "format": "pgn",
        "text": "1. e4 e5 2. Nf3 Nc6 *",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["moves_uci"] == ["e2e4", "e7e5", "g1f3", "b8c6"]
    assert body["side_to_move"] == "white"


def test_validate_endpoint_unknown_format(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={"format": "epd", "text": "anything"})
    assert r.status_code == 400


def test_validate_endpoint_auto_detects_fen(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={
        "format": "auto",
        "text": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["detected_format"] == "fen"
    assert body["moves_uci"] == []


def test_validate_endpoint_auto_detects_pgn(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={
        "format": "auto",
        "text": "1. e4 e5 2. Nf3 *",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["detected_format"] == "pgn"
    assert body["moves_uci"] == ["e2e4", "e7e5", "g1f3"]


def test_validate_endpoint_auto_returns_helpful_error_when_neither(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={"format": "auto", "text": "xyzzy"})
    assert r.status_code == 400
    assert "FEN" in r.json()["detail"] or "PGN" in r.json()["detail"]


def test_validate_endpoint_default_format_is_auto(client):
    c, _app, _ = client
    r = c.post("/game/import/validate", json={
        "text": "1. e4 e5 *",
    })
    assert r.status_code == 200
    assert r.json()["detected_format"] == "pgn"


def test_import_endpoint_lands_in_view_mode_at_last_ply(client):
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "fen",
        "text": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    })
    assert r.status_code == 200, r.text
    assert r.json()["viewing"] is True
    assert hve._viewing is True
    assert hve._board is not None
    assert hve._board.turn == chess.BLACK
    # No play-mode side effects yet: engine isn't kicked, no new game.
    hve._engine_to_move.assert_not_called()


def test_view_play_from_here_inherits_side_to_move(client):
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    # Black to move at the cursor → human plays Black on play-from-here.
    c.post("/game/import", json={
        "format": "fen",
        "text": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    })
    r = c.post("/game/view/play-from-here", json={})
    assert r.status_code == 200, r.text
    assert r.json()["viewing"] is False
    assert hve._viewing is False
    assert hve._human_white is False  # side-to-move was Black


def test_import_endpoint_pgn_replays_into_history(client):
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "pgn",
        "text": "1. e4 e5 2. Nf3 Nc6 *",
    })
    assert r.status_code == 200, r.text
    # View mode lands at the last ply with all moves replayed.
    assert hve._viewing is True
    assert [m.uci() for m in hve._view_full_moves] == [
        "e2e4", "e7e5", "g1f3", "b8c6",
    ]
    assert hve._view_cursor == 4
    hve._engine_to_move.assert_not_called()


def test_view_play_from_here_kicks_engine_when_engine_to_move(client):
    """When the cursor's side-to-move is the engine's color, play_from_here
    must kick the engine. Side here is implicitly inherited from the cursor."""
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    # 1.e4 → Black to move. side-to-move at cursor is Black → human plays
    # Black, so engine plays White and is NOT to move. Build a different
    # scenario: cursor at startpos (ply 0), side-to-move is White → human
    # plays White, engine is Black, no kick. To trigger a kick we need a
    # cursor position where the engine (= the side opposite human) is to
    # move. Since human is always the cursor's side-to-move, the engine is
    # never to move right after play_from_here. So the engine kick happens
    # only later, after human's first move. Confirm no kick at this stage.
    c.post("/game/import", json={
        "format": "fen",
        "text": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
    })
    c.post("/game/view/play-from-here", json={})
    # Side-to-move at cursor was Black → human is Black. Engine is White
    # but it's Black's turn (the human's turn) — engine NOT kicked.
    hve._engine_to_move.assert_not_called()


def test_import_endpoint_accepts_finished_position(client):
    """Finished games (checkmate, stalemate, draw) are loaded into view
    mode at the last ply for post-mortem inspection."""
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "pgn",
        "text": "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0",
    })
    assert r.status_code == 200, r.text
    assert r.json()["viewing"] is True
    assert hve._viewing is True
    assert hve._view_cursor == 7  # 7 plies, cursor at the last
    assert hve._board.is_checkmate()


def test_imported_game_publishes_board_event_after_engine_move(client):
    """Regression: _moves_san used to replay on a fresh chess.Board() and
    asserted out for any imported (non-startpos) game once the engine moved.
    The crash silently aborted the publish, leaving the client frozen.

    Now that import → view mode, exit via play-from-here first so this
    exercises the play-mode _board_event code path the bug was in.
    """
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    c.post("/game/import", json={
        "format": "fen",
        "text": "1k1r4/pp1b1R2/3q2pp/4p3/2B5/4Q3/PPP2B2/2K5 b - -",
    })
    c.post("/game/view/play-from-here", json={})
    # Simulate the engine's bestmove being pushed (the part of
    # _think_and_play that runs after the search returns).
    hve._board.push(chess.Move.from_uci("d6d1"))
    # _board_event must not raise.
    evt = hve._board_event()
    assert evt.payload["fen"].startswith("1k1r")
    # moves_san reflects the imported-then-engine-played sequence.
    assert evt.payload["moves_san"] == ["Qd1+"]


def test_view_play_from_here_honors_clk_annotations(client):
    """Import a PGN with [%clk] then play-from-here at the last ply; live
    clocks reflect the parsed values, not the TC initial."""
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    c.post("/game/import", json={
        "format": "pgn",
        "text": (
            "1. e4 { [%clk 0:04:55] } 1... c5 { [%clk 0:04:50] } "
            "2. Nf3 { [%clk 0:04:48] } *"
        ),
    })
    r = c.post("/game/view/play-from-here", json={
        "initial_seconds": 300,
        "increment_seconds": 0,
    })
    assert r.status_code == 200, r.text
    # Live clocks come from the PGN, not the configured TC initial.
    assert hve._white_time == 4 * 60 + 48
    assert hve._black_time == 4 * 60 + 50
    # _clock_history has one entry per seeded ply with PGN-derived snapshots
    # (None entries fill from TC initial = 300).
    assert hve._clock_history == [
        (300.0, 300.0),  # ply 0: nobody moved yet
        (4 * 60 + 55, 300.0),  # ply 1: only white moved
        (4 * 60 + 55, 4 * 60 + 50),  # ply 2: both moved
    ]


def test_view_play_from_here_seed_clocks_use_configured_tc(client):
    """A PGN without [%clk] then play-from-here falls back to TC initial."""
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    c.post("/game/import", json={
        "format": "pgn",
        "text": "1. e4 e5 *",
    })
    r = c.post("/game/view/play-from-here", json={
        "initial_seconds": 123.0,
        "increment_seconds": 4.0,
    })
    assert r.status_code == 200, r.text
    assert hve._tc.initial_seconds == 123.0
    assert hve._tc.increment_seconds == 4.0
    assert hve._white_time == 123.0
    assert hve._black_time == 123.0
