"""`material` tool tests.

Pure function of the supplied FEN -- no engine, no live board. Counts
pieces per color, keyed by name, kings omitted. Error paths mirror
`analyze` (missing_fen / invalid_fen) since both share `_parse_fen_arg`.
"""
from __future__ import annotations

import pytest

from sturddle_view.llm.cancel import CancelToken
from sturddle_view.play.tools_engine import make_material_tool


@pytest.mark.asyncio
async def test_material_startpos_counts():
    material = make_material_tool()
    out = await material({"fen": "startpos"}, cancel_token=CancelToken())

    assert "error" not in out, out
    assert out["white"] == {"pawn": 8, "knight": 2, "bishop": 2, "rook": 2, "queen": 1}
    assert out["black"] == {"pawn": 8, "knight": 2, "bishop": 2, "rook": 2, "queen": 1}
    # Kings are omitted -- always one per side, no material signal.
    assert "king" not in out["white"]


@pytest.mark.asyncio
async def test_material_per_piece_breakdown():
    # Knight-for-bishop swap: counts are equal in total, but white has
    # 2N/1B and black 1N/2B -- pins that the per-piece breakdown (not just
    # a total) is reported correctly. Hand-built so the counts are exact.
    fen = "r1bqk2r/pppp1ppp/2n5/8/1b6/2N2N2/PPPP1PPP/R1BQ1RK1 w kq - 0 1"
    material = make_material_tool()
    out = await material({"fen": fen}, cancel_token=CancelToken())

    assert "error" not in out, out
    assert out["white"] == {"pawn": 7, "knight": 2, "bishop": 1, "rook": 2, "queen": 1}
    assert out["black"] == {"pawn": 7, "knight": 1, "bishop": 2, "rook": 2, "queen": 1}


@pytest.mark.asyncio
async def test_material_strips_whitespace_padded_startpos():
    # '  startpos  ' must parse like 'startpos' (stripped before _parse_fen).
    material = make_material_tool()
    out = await material({"fen": "  startpos  "}, cancel_token=CancelToken())
    assert "error" not in out, out
    assert out["white"]["pawn"] == 8


@pytest.mark.asyncio
async def test_material_whitespace_only_fen_is_missing():
    material = make_material_tool()
    out = await material({"fen": "   "}, cancel_token=CancelToken())
    assert out.get("error") == "missing_fen"


@pytest.mark.asyncio
async def test_material_missing_fen():
    material = make_material_tool()
    out = await material({}, cancel_token=CancelToken())
    assert out.get("error") == "missing_fen"


@pytest.mark.asyncio
async def test_material_invalid_fen():
    material = make_material_tool()
    out = await material({"fen": "not-a-fen"}, cancel_token=CancelToken())
    assert out.get("error") == "invalid_fen"
    assert "detail" in out
