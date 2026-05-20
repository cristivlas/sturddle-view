"""Tests for get_pgn_text() -- the export-to-filesystem feature."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine, ViewModeParams
from sturddle_view.play.import_position import parse_pgn

FIXTURES = Path(__file__).parent / "fixtures"


class _StubEngine:
    def send_line(self, _line: str) -> None:
        pass

    async def quit(self) -> None:
        return None


@pytest.fixture
def hve():
    settings = Settings()
    bus = EventBus()
    h = HumanVsEngine(engine_path="/nonexistent", bus=bus, settings=settings)

    async def fake_ensure_engine():
        h._engine = _StubEngine()
        return h._engine

    h._ensure_engine = fake_ensure_engine
    h._engine_to_move = AsyncMock()
    return h


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


async def _enter_view_from_pgn(h: HumanVsEngine, raw: str, view_hash: str) -> None:
    parsed = parse_pgn(raw)
    headers = parsed.headers or {}
    await h.enter_view_mode(ViewModeParams(
        start_fen=parsed.start_fen,
        moves_uci=parsed.moves_uci,
        clock_history=parsed.clock_history,
        final_white_time=parsed.final_white_time,
        final_black_time=parsed.final_black_time,
        white_name=headers.get("White"),
        black_name=headers.get("Black"),
        eval_history=parsed.eval_history,
        comments=parsed.comments,
        root_comment=parsed.root_comment,
        pgn_result=headers.get("Result"),
        pgn_termination=headers.get("Termination"),
        view_hash=view_hash,
        view_summary=parsed.summary,
        view_raw_text=raw,
    ))


async def test_no_game_returns_none(hve):
    result = hve.get_pgn_text()
    assert result is None


async def test_play_mode_no_moves_returns_none(hve):
    from sturddle_view.play.human_vs_engine import TimeControl
    await hve.new_game(human_white=True, tc=TimeControl(60, 0))
    assert hve.get_pgn_text() is None


async def test_fen_only_view_returns_none(hve):
    """A FEN import has no moves and no verbatim PGN -- nothing to export."""
    await hve.enter_view_mode(ViewModeParams(
        start_fen="rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1",
        moves_uci=[],
        clock_history=None,
        view_hash="abc123",
        view_raw_text=None,  # API only sets this for PGN imports, not FEN
    ))
    assert hve.get_pgn_text() is None


async def test_zero_move_pgn_with_fen_header_returns_verbatim(hve):
    """PGN that starts from a custom FEN with no moves: verbatim return, not None."""
    # parse_pgn accepts this because it has a FEN header (non-startpos).
    fen = "rnbqkbnr/pppp1ppp/8/4p3/4P3/8/PPPP1PPP/RNBQKBNR w KQkq e6 0 2"
    raw = f'[Event "Test"]\n[SetUp "1"]\n[FEN "{fen}"]\n[Result "*"]\n\n*\n'
    await _enter_view_from_pgn(hve, raw, "aabb")
    result = hve.get_pgn_text()
    assert result is not None
    pgn_text, _ = result
    assert pgn_text == raw


async def test_play_mode_export(hve):
    """In-progress play game: result=*, termination=unterminated, moves present."""
    from sturddle_view.play.human_vs_engine import TimeControl
    await hve.new_game(human_white=True, tc=TimeControl(60, 0))
    await hve.submit_move("e2e4")

    result = hve.get_pgn_text()
    assert result is not None
    pgn_text, filename = result

    assert '[Result "*"]' in pgn_text
    assert '[Termination "unterminated"]' in pgn_text
    assert "1. e4" in pgn_text
    assert filename.endswith(".pgn")


async def test_view_without_hash_falls_back_to_rebuilt_pgn(hve):
    """View entered via view/start (no raw text) rebuilds from move list."""
    await hve.enter_view_mode(ViewModeParams(
        start_fen=None,
        moves_uci=["e2e4", "e7e5", "g1f3"],
        clock_history=None,
        white_name="Alice",
        black_name="Bob",
        pgn_result="*",
        view_hash=None,
        view_raw_text=None,
    ))

    result = hve.get_pgn_text()
    assert result is not None
    pgn_text, filename = result

    assert "1. e4" in pgn_text
    assert "Alice" in pgn_text
    assert "Bob" in pgn_text
    assert filename.endswith(".pgn")


async def test_view_raw_text_survives_edit_cancel(hve):
    """view_raw_text must be restored after entering and cancelling edit mode."""
    raw = _load_fixture("Fischer vs Bolbochan, Stockholm 1962.pgn")
    await _enter_view_from_pgn(hve, raw, "deadbeef")

    await hve.enter_edit_mode()
    await hve.cancel_edit()

    result = hve.get_pgn_text()
    assert result is not None
    pgn_text, _ = result
    assert pgn_text == raw


@pytest.mark.parametrize("filename,must_contain", [
    (
        "Fischer vs Bolbochan, Stockholm 1962.pgn",
        [
            '[Event "Stockholm Interzonal"]',
            '[White "Robert James Fischer"]',
            "Notes by Bobby Fischer",
            "Amateurs are often puzzled",
            "The coup de grace",
            "1-0",
        ],
    ),
    (
        "Fischer vs Petrosian, Herceg Novi 1970.pgn",
        [
            '[Event "Herceg Novi blitz"]',
            '[White "Robert James Fischer"]',
            '[Black "Tigran Vartanovich Petrosian"]',
            "Notes by Bobby Fischer",
            "$2",   # NAG
            "$4",   # NAG
            "1-0",
        ],
    ),
])
async def test_view_export_with_hash_returns_verbatim_pgn(hve, filename, must_contain):
    """get_pgn_text() in view mode with a known hash must return the
    original raw PGN bytes exactly -- no metadata loss."""
    raw = _load_fixture(filename)
    await _enter_view_from_pgn(hve, raw, "deadbeef")

    result = hve.get_pgn_text()
    assert result is not None
    pgn_text, dl_filename = result

    assert pgn_text == raw
    for token in must_contain:
        assert token in pgn_text
    assert dl_filename.endswith(".pgn")
    assert "Fischer" in dl_filename


async def test_play_from_here_lossy_export(hve):
    """After play_from_here, view_raw_text is gone; export rebuilds from live board."""
    raw = _load_fixture("Fischer vs Bolbochan, Stockholm 1962.pgn")
    await _enter_view_from_pgn(hve, raw, "deadbeef")

    from sturddle_view.play.human_vs_engine import TimeControl
    await hve.play_from_here(tc=TimeControl(60, 0))

    result = hve.get_pgn_text()
    # No moves yet in the new game -- export returns None.
    assert result is None

    await hve.submit_move("e2e4")
    result = hve.get_pgn_text()
    assert result is not None
    pgn_text, filename = result
    # Rebuilt PGN: must NOT contain Fischer's annotations or original Event.
    assert "Notes by Bobby Fischer" not in pgn_text
    assert "Stockholm Interzonal" not in pgn_text
    assert "1. e4" in pgn_text
    assert filename.endswith(".pgn")


@pytest.fixture
def api_client(tmp_path):
    fake_engine = tmp_path / "engine"
    fake_engine.write_text("")
    settings = Settings(token="t", auth_disabled=True)
    settings.engine_path = fake_engine
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)

    hve = HumanVsEngine(
        engine_path=str(fake_engine),
        bus=app.state.event_bus,
        openings=getattr(app.state, "openings", None),
        settings=app.state.settings,
    )

    async def _fake_ensure_engine():
        hve._engine = _StubEngine()
        return hve._engine

    hve._ensure_engine = _fake_ensure_engine
    hve._engine_to_move = AsyncMock()
    # lifespan sets app.state.hve = None (no saved game in tmp_path);
    # overwrite after TestClient enters so the lifespan has already run.
    with TestClient(app) as c:
        app.state.hve = hve
        yield c


def test_export_pgn_endpoint_import_then_get(api_client):
    """GET /game/pgn after a PGN import must return the verbatim file."""
    raw = (FIXTURES / "Fischer vs Bolbochan, Stockholm 1962.pgn").read_text(encoding="utf-8")
    r = api_client.post("/game/import", json={"text": raw})
    assert r.status_code == 200

    r = api_client.get("/game/pgn")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-chess-pgn")
    assert "charset=utf-8" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    assert ".pgn" in r.headers["content-disposition"]
    assert r.text == raw