"""TablebaseProber unit tests.

Positions verified against python-chess syzygy on a 3-4-5 man TB set:
  KNBvK   8/2K5/4B3/3N4/8/8/4k3/8 b - - 0 1   wdl=-2 (loss for STM)
  KNNvKP  1N6/8/p7/8/8/8/2k1N3/K7 w - - 0 1   wdl=1  (cursed win)
  KPPvKP  8/8/1k2P2K/6P1/8/3p4/8/8 b - - 0 1  wdl=-1 (blessed loss)

Run with --syzygy-path=/path/to/tables or set SYZYGY_PATH env var.
"""
from __future__ import annotations

import os
import pytest
import chess

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.play.tablebase import TablebaseProber, MAX_PIECES


@pytest.fixture(scope="session")
def syzygy_path(request):
    path = request.config.getoption("--syzygy-path") or os.environ.get("SYZYGY_PATH")
    if not path:
        pytest.skip("pass --syzygy-path or set SYZYGY_PATH to run tablebase tests")
    return path


@pytest.fixture
def prober(syzygy_path):
    tb = TablebaseProber(syzygy_path)
    yield tb
    tb.close()


# ---- probe results ----

def test_probe_knb_vs_k_loss(prober):
    board = chess.Board("8/2K5/4B3/3N4/8/8/4k3/8 b - - 0 1")
    result = prober.probe(board)
    assert result is not None
    assert result["wdl"] == -2
    assert "dtz" in result


def test_probe_knn_vs_kp_cursed_win(prober):
    board = chess.Board("1N6/8/p7/8/8/8/2k1N3/K7 w - - 0 1")
    result = prober.probe(board)
    assert result is not None
    assert result["wdl"] == 1
    assert "dtz" in result


def test_probe_kpp_vs_kp_blessed_loss(prober):
    board = chess.Board("8/8/1k2P2K/6P1/8/3p4/8/8 b - - 0 1")
    result = prober.probe(board)
    assert result is not None
    assert result["wdl"] == -1
    assert "dtz" in result


# ---- guard conditions ----

def test_probe_returns_none_when_game_over(prober):
    # Stalemate position -- game is already over.
    board = chess.Board("k7/8/1Q6/8/8/8/8/K7 b - - 0 1")
    assert prober.probe(board) is None


def test_probe_returns_none_too_many_pieces(prober):
    # Starting position has 32 pieces -- well above MAX_PIECES.
    board = chess.Board()
    assert sum(1 for _ in board.piece_map()) > MAX_PIECES
    assert prober.probe(board) is None


def test_probe_missing_table_returns_none(prober):
    # 7-man position -- not in a 3-4-5 TB set.
    board = chess.Board("8/8/3r4/8/8/8/6R1/NKBk1b2 w - - 0 1")
    assert prober.probe(board) is None


# ---- _ensure_tablebase path-change / clear ----

def test_ensure_tablebase_clears_prober_when_path_removed(syzygy_path):
    settings = Settings(token="t", auth_disabled=True, engine_default_syzygy_path=syzygy_path)
    hve = HumanVsEngine(engine_path="/nonexistent", bus=EventBus(), settings=settings)
    hve._ensure_tablebase()
    assert hve._tb is not None
    settings.engine_default_syzygy_path = None
    hve._ensure_tablebase()
    assert hve._tb is None


# ---- halfmove_clock not in probe result (caller adds it) ----

def test_probe_result_has_no_halfmove_clock_field(prober):
    board = chess.Board("8/2K5/4B3/3N4/8/8/4k3/8 b - - 0 1")
    result = prober.probe(board)
    assert result is not None
    assert "halfmove_clock" not in result
