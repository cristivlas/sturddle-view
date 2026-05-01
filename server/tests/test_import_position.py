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


def test_parse_fen_finished_position_rejected():
    # Fool's mate position: White just got mated.
    fen = "rnb1kbnr/pppp1ppp/8/4p3/6Pq/5P2/PPPPP2P/RNBQKBNR w KQkq - 1 3"
    with pytest.raises(PositionImportError, match="already over"):
        parse_fen(fen)


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
    assert p.start_fen == chess.Board().fen()
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


def test_parse_pgn_finished_game_rejected():
    pgn = "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0"
    with pytest.raises(PositionImportError, match="finished"):
        parse_pgn(pgn)


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


def test_import_endpoint_starts_game_human_as_side_to_move(client):
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "fen",
        "text": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        "human_side": "side_to_move",
    })
    assert r.status_code == 200, r.text
    assert r.json()["human_white"] is False  # side_to_move was Black
    assert hve._board is not None
    assert hve._board.turn == chess.BLACK
    # Engine kick is not expected — it's the human's turn.
    hve._engine_to_move.assert_not_called()


def test_import_endpoint_pgn_replays_into_history(client):
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "pgn",
        "text": "1. e4 e5 2. Nf3 Nc6 *",
        "human_side": "white",  # White's turn after Nc6 → human moves next.
    })
    assert r.status_code == 200, r.text
    # Move stack should be populated, so the move panel renders the imported
    # game and engine-side TB / repetition detection works.
    assert [m.uci() for m in hve._board.move_stack] == [
        "e2e4", "e7e5", "g1f3", "b8c6",
    ]
    assert hve._human_white is True
    hve._engine_to_move.assert_not_called()


def test_import_endpoint_kicks_engine_when_its_turn(client):
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    # Black to move; human picks White → engine should immediately think.
    r = c.post("/game/import", json={
        "format": "fen",
        "text": "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq - 0 1",
        "human_side": "white",
    })
    assert r.status_code == 200, r.text
    assert r.json()["human_white"] is True
    hve._engine_to_move.assert_called_once()


def test_import_endpoint_rejects_finished_position(client):
    c, app, engine_path = client
    _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "pgn",
        "text": "1. e4 e5 2. Qh5 Nc6 3. Bc4 Nf6 4. Qxf7# 1-0",
    })
    assert r.status_code == 400


def test_imported_game_publishes_board_event_after_engine_move(client):
    """Regression: _moves_san used to replay on a fresh chess.Board() and
    asserted out for any imported (non-startpos) game once the engine moved.
    The crash silently aborted the publish, leaving the client frozen."""
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    # Black to move; human picks Black (so it's still Black to move and we
    # can simulate an engine reply by directly invoking _board_event after
    # pushing a move that's only legal in this position).
    r = c.post("/game/import", json={
        "format": "fen",
        "text": "1k1r4/pp1b1R2/3q2pp/4p3/2B5/4Q3/PPP2B2/2K5 b - -",
        "human_side": "white",  # so engine is on the move (Black)
    })
    assert r.status_code == 200, r.text
    # Simulate the engine's bestmove being pushed (the part of
    # _think_and_play that runs after the search returns).
    hve._board.push(chess.Move.from_uci("d6d1"))
    # _board_event must not raise.
    evt = hve._board_event()
    assert evt.payload["fen"].startswith("1k1r")
    # moves_san reflects the imported-then-engine-played sequence.
    assert evt.payload["moves_san"] == ["Qd1+"]


def test_import_endpoint_seed_clocks_use_configured_tc(client):
    """Imported positions start fresh at the configured TC (no PGN clock
    comments honored). Verifies the settings/payload override path."""
    c, app, engine_path = client
    hve = _patch_hve(app, engine_path)
    r = c.post("/game/import", json={
        "format": "pgn",
        "text": "1. e4 e5 *",
        "initial_seconds": 123.0,
        "increment_seconds": 4.0,
    })
    assert r.status_code == 200, r.text
    assert hve._tc.initial_seconds == 123.0
    assert hve._tc.increment_seconds == 4.0
    assert hve._white_time == 123.0
    assert hve._black_time == 123.0
