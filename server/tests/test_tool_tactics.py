"""`tactics` tool tests.

Pure function of the supplied FEN -- no engine, no live board. Lists pins
(king/queen shields) and forks for both colors with pieces named by color
and square. Error paths mirror `material` (shared `_parse_fen_arg`).
"""
from __future__ import annotations

import pytest

from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_engine import make_tactics_tool


# Bb5 pins the c6 knight to the e8 king; no fork.
_RUY_PIN_FEN = "r1bqkbnr/ppp2ppp/2np4/1B2p3/4P3/5N2/PPPP1PPP/RNBQK2R w KQkq - 0 4"
# Nc7+ forks the a8 rook and the e8 king; no pin.
_KNIGHT_FORK_FEN = "r3k3/2N5/8/8/8/8/8/4K3 b - - 0 1"


@pytest.mark.asyncio
async def test_tactics_startpos_is_empty():
    tactics = make_tactics_tool()
    out = await tactics({"fen": "startpos"}, cancel_token=CancelToken())
    assert out == {"pins": [], "forks": []}


@pytest.mark.asyncio
async def test_tactics_reports_pin_by_color_and_square():
    tactics = make_tactics_tool()
    out = await tactics({"fen": _RUY_PIN_FEN}, cancel_token=CancelToken())
    assert out["pins"] == [{
        "pinned": "black knight on c6",
        "by": "white bishop on b5",
        "to": "black king on e8",
    }]
    assert out["forks"] == []


@pytest.mark.asyncio
async def test_tactics_reports_fork_targets():
    tactics = make_tactics_tool()
    out = await tactics({"fen": _KNIGHT_FORK_FEN}, cancel_token=CancelToken())
    assert out["pins"] == []
    assert out["forks"] == [{
        "by": "white knight on c7",
        "targets": ["black rook on a8", "black king on e8"],
    }]


@pytest.mark.asyncio
async def test_tactics_missing_and_invalid_fen():
    tactics = make_tactics_tool()
    assert (await tactics({}, cancel_token=CancelToken()))["error"] == "missing_fen"
    assert (await tactics({"fen": "nope"}, cancel_token=CancelToken()))["error"] == "invalid_fen"
