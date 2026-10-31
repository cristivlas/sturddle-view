"""`test_aim` tool: the verifier attacks the delegated idea's aim.

Pins docs/ai-analysis-skin-in-the-game.md item 7:

- a reply far below the opponent's best is unsound and tests nothing,
- after a sound reply the free line is the reply search's pv, read for
  AIM_HORIZON (3) moves of the tested side; at each where the aim is legal it is
  forced, and the best attempt is scored against the line,
- refuted when the aim is legal nowhere on that stretch, or its best attempt
  trails the line by more than PLAY_OUT_MARGIN_CP,
- at most VERIFIER_MAX_REPLIES replies per delegate; no panel progress steps.

Move under test throughout: 1.e4 from the start, aim d4. Searches come from
a scripted _run_one_search (side-to-move POV).
"""
from __future__ import annotations

import chess
import chess.engine
import pytest

from sturddle_view.events import EventBus
from sturddle_view.llm.cancel import CancelToken
from sturddle_view.llm.tool_progress import reporting_progress
from sturddle_view.play import tools_engine
from sturddle_view.play.tools_engine import (
    VERIFIER_MAX_REPLIES,
    AimTest,
    make_test_aim_tool,
    normalize_aim,
)

from .scripted_search import epd_after, scripted_search


_E4 = chess.Move.from_uci("e2e4")
# After 1.e4: the opponent's best is ...e5 (level). The free line runs
# 2.Nf3 Nc6 3.d4 exd4: d4 is legal at White's 2nd and 3rd moves.
_AFTER_E4 = (epd_after("e4"), None)
_FREE_LINE = ["e7e5", "g1f3", "b8c6", "d2d4", "e5d4"]


def _script() -> dict:
    return {
        _AFTER_E4: (0, _FREE_LINE),
        (epd_after("e4"), "a7a6"): (-60, ["a7a6", "d2d4"]),
        (epd_after("e4", "e5"), "d2d4"): (10, ["d2d4"]),
        (epd_after("e4", "e5", "Nf3", "Nc6"), "d2d4"): (20, ["d2d4"]),
    }


def _tool(monkeypatch, aim: str = "d4", script=None, calls=None, test: AimTest | None = None):
    calls = [] if calls is None else calls
    monkeypatch.setattr(
        tools_engine, "_run_one_search", scripted_search(script or _script(), calls),
    )
    board = chess.Board()
    test = test or AimTest(_E4, aim)
    return make_test_aim_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board, aim_provider=lambda: test,
    )


async def _reply(tool, reply: str) -> dict:
    return await tool({"reply": reply}, cancel_token=CancelToken())


@pytest.mark.asyncio
async def test_sound_reply_the_aim_survives(monkeypatch):
    calls: list = []
    out = await _reply(_tool(monkeypatch, calls=calls), "e5")
    assert out == {
        "reply": "e5", "sound": True, "refuted": False,
        "line": "1.e4 e5 2.Nf3 Nc6 3.d4 exd4", "aim_at": "3.d4",
    }
    # The best reply IS ...e5, so its search stands in for the reply's own;
    # then one forced branch per tested-side move where d4 is legal.
    assert calls == [
        _AFTER_E4,
        (epd_after("e4", "e5"), "d2d4"),
        (epd_after("e4", "e5", "Nf3", "Nc6"), "d2d4"),
    ]


@pytest.mark.asyncio
async def test_unsound_reply_tests_nothing(monkeypatch):
    calls: list = []
    out = await _reply(_tool(monkeypatch, calls=calls), "a6")
    assert out["sound"] is False and "unsound" in out["reason"]
    assert "line" not in out and "refuted" not in out
    assert calls == [_AFTER_E4, (epd_after("e4"), "a7a6")]


@pytest.mark.asyncio
async def test_aim_never_legal_is_refuted(monkeypatch):
    # The f1 bishop never leaves along the line: no castling.
    out = await _reply(_tool(monkeypatch, aim="O-O"), "e5")
    assert out["refuted"] is True
    assert "never becomes playable" in out["reason"]
    assert "aim_at" not in out


@pytest.mark.asyncio
async def test_aim_that_falls_short_of_the_line_is_refuted(monkeypatch):
    # The line is level; d4's best attempt trails it by one cp too many.
    shortfall = tools_engine.PLAY_OUT_MARGIN_CP + 1
    script = _script()
    script[(epd_after("e4", "e5"), "d2d4")] = (-shortfall, ["d2d4"])
    script[(epd_after("e4", "e5", "Nf3", "Nc6"), "d2d4")] = (-shortfall - 5, ["d2d4"])
    out = await _reply(_tool(monkeypatch, script=script), "e5")
    assert out["refuted"] is True
    assert "falls short" in out["reason"]
    assert out["aim_at"] == "2.d4"


@pytest.mark.asyncio
async def test_attempt_within_the_margin_holds(monkeypatch):
    script = _script()
    script[(epd_after("e4", "e5"), "d2d4")] = (-tools_engine.PLAY_OUT_MARGIN_CP, ["d2d4"])
    script[(epd_after("e4", "e5", "Nf3", "Nc6"), "d2d4")] = (-200, ["d2d4"])
    out = await _reply(_tool(monkeypatch, script=script), "e5")
    assert out["refuted"] is False
    assert out["aim_at"] == "2.d4"


@pytest.mark.asyncio
async def test_line_ending_early_with_the_aim_never_legal_is_untested(monkeypatch):
    # The pv stops after ...e5: castling is not legal yet, and the missing
    # moves might have allowed it. That says nothing about the aim.
    script = _script()
    script[_AFTER_E4] = (0, ["e7e5"])
    out = await _reply(_tool(monkeypatch, aim="O-O", script=script), "e5")
    assert out["untested"] is True and out["refuted"] is False
    assert "another reply" in out["reason"]


@pytest.mark.asyncio
async def test_full_line_with_the_aim_never_legal_is_refuted_not_untested(monkeypatch):
    out = await _reply(_tool(monkeypatch, aim="O-O"), "e5")
    assert out["refuted"] is True and "untested" not in out


@pytest.mark.parametrize("aim", ["O-O", "d4"])
@pytest.mark.asyncio
async def test_line_that_mates_for_the_tested_side_holds(monkeypatch, aim):
    # The line's search reports mate for White (Black, to move, is mated
    # in 5): never-legal (O-O) or falling short (d4 forced at -500), the
    # idea won outright.
    script = _script()
    script[_AFTER_E4] = (chess.engine.Mate(-5), _FREE_LINE)
    script[(epd_after("e4", "e5"), "d2d4")] = (-500, ["d2d4"])
    script[(epd_after("e4", "e5", "Nf3", "Nc6"), "d2d4")] = (-500, ["d2d4"])
    out = await _reply(_tool(monkeypatch, aim=aim, script=script), "e5")
    assert out["refuted"] is False and "mates" in out["reason"], out


@pytest.mark.asyncio
async def test_short_pv_shortens_the_horizon(monkeypatch):
    calls: list = []
    script = _script()
    script[_AFTER_E4] = (0, ["e7e5"])
    out = await _reply(_tool(monkeypatch, script=script, calls=calls), "e5")
    assert out["line"] == "1.e4 e5"
    assert calls == [_AFTER_E4, (epd_after("e4", "e5"), "d2d4")]


@pytest.mark.asyncio
async def test_replies_are_capped_per_delegate(monkeypatch):
    test = AimTest(_E4, "d4")
    tool = _tool(monkeypatch, test=test)
    for _ in range(VERIFIER_MAX_REPLIES):
        assert "error" not in await _reply(tool, "e5")
    out = await _reply(tool, "e5")
    assert out["error"] == "replies_spent"


@pytest.mark.asyncio
async def test_illegal_reply_does_not_count(monkeypatch):
    test = AimTest(_E4, "d4")
    out = await _reply(_tool(monkeypatch, test=test), "e4")  # White's move
    assert out["error"] == "illegal_move"
    assert test.replies == 0


@pytest.mark.asyncio
async def test_no_move_under_test_outside_a_delegate(monkeypatch):
    monkeypatch.setattr(tools_engine, "_run_one_search", scripted_search(_script(), []))
    board = chess.Board()
    tool = make_test_aim_tool(
        lambda: None, bus=EventBus(), board_provider=lambda: board, aim_provider=lambda: None,
    )
    out = await _reply(tool, "e5")
    assert out["error"] == "no_move_under_test"


@pytest.mark.asyncio
async def test_test_aim_reports_no_panel_progress_steps(monkeypatch):
    steps = []

    async def reporter(name, input_):
        steps.append(name)

        async def finish(_output):
            return None
        return finish

    with reporting_progress(reporter):
        await _reply(_tool(monkeypatch), "a6")
    assert steps == []


@pytest.mark.parametrize(("raw", "aim"), [
    ("d4", "d4"), ("7.d4", "d4"), ("7...Nf6", "Nf6"), (" O-O ", "O-O"),
    ("e7e8q", "e7e8q"), ("the d4 break", None), ("", None),
])
def test_normalize_aim(raw, aim):
    assert normalize_aim(raw) == aim
