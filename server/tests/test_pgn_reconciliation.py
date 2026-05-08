"""PGN reconciliation: move-list capture (slice 1).

Verifies that the orchestrator captures the cumulative UCI move list
of each confirmed pair, updates it from `position startpos moves ...`
lines emitted by either side, and tears it down at dissolution and
on terminal runner events.

Slices 2-3 add the PGN tailer and the match queue + `game_reconciled`
event; their tests will live alongside these.

See `docs/pgn-reconciliation.md`.
"""
from __future__ import annotations

import pytest

from sturddle_view.tournament.orchestrator import Orchestrator
from sturddle_view.tournament.runner import RunSpec
from sturddle_view.tournament.store import TournamentStore


_ENGINE_A = "Engine A"
_ENGINE_B = "Engine B"
_PROXY_A  = "proxy-a"
_PROXY_B  = "proxy-b"


class _FakeRunner:
    binary_path = "/fake/fastchess"
    def is_running(self) -> bool: return False
    async def start(self, spec: RunSpec, on_event) -> None: pass  # noqa: ARG002
    async def stop(self) -> None: pass


@pytest.fixture
def orch(tmp_path):
    store = TournamentStore(tmp_path / "tournaments")
    o = Orchestrator(store, _FakeRunner())
    o._active_id = "tournament-x"
    return o


async def _confirm_pair(orch, pid_white: str, pid_black: str) -> None:
    """Same minimal rendezvous used in test_tournament_game_lifecycle."""
    await orch.ingest_proxy_lines(pid_white, [
        "position startpos",
        "bestmove e2e4",
    ])
    await orch.ingest_proxy_lines(pid_black, [
        "position startpos moves e2e4",
    ])


def _pair_id(orch) -> str:
    assert len(orch._pair_ids) == 1
    return next(iter(orch._pair_ids.values()))


@pytest.mark.asyncio
async def test_pair_moves_initialized_on_confirmation(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    pair_id = _pair_id(orch)
    # Confirmation rendezvous already passed one ply through update.
    # The list exists; content is "what we've seen so far" (= ["e2e4"]).
    assert pair_id in orch._pair_moves
    assert orch._pair_moves[pair_id] == ["e2e4"]


@pytest.mark.asyncio
async def test_pair_moves_grows_with_position_lines(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)

    # White receives position with the new ply, then plays its move.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4 e7e5",
        "bestmove g1f3",
    ])
    await orch.ingest_proxy_lines(_PROXY_B, [
        "position startpos moves e2e4 e7e5 g1f3",
    ])

    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]


@pytest.mark.asyncio
async def test_shorter_or_stale_position_does_not_clobber(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)

    # Drive the list forward to 3 plies.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4 e7e5",
    ])
    await orch.ingest_proxy_lines(_PROXY_B, [
        "position startpos moves e2e4 e7e5 g1f3",
    ])
    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]

    # A late-arriving shorter frame from White (already covered by
    # what Black reported) must not shrink the list.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4 e7e5",
    ])
    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]

    # A divergent (non-prefix) shorter list is also rejected. Synthetic
    # — a real engine wouldn't send this, but the guard belongs in the
    # update code so book-line collisions early in the game can't poison
    # state once it has diverged.
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves d2d4",
    ])
    assert orch._pair_moves[pair_id] == ["e2e4", "e7e5", "g1f3"]


@pytest.mark.asyncio
async def test_pair_moves_dropped_on_dissolution(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)
    assert pair_id in orch._pair_moves

    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    assert pair_id not in orch._pair_moves


@pytest.mark.asyncio
async def test_pair_moves_dropped_on_proxy_session_end(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = _pair_id(orch)
    assert pair_id in orch._pair_moves

    await orch.proxy_session_ended(_PROXY_A)

    assert pair_id not in orch._pair_moves


@pytest.mark.asyncio
async def test_pair_moves_cleared_by_reset_state(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    assert orch._pair_moves

    orch._reset_pairing_state()

    assert orch._pair_moves == {}


@pytest.mark.asyncio
async def test_position_outside_confirmed_pair_is_ignored(orch):
    """Lines from a proxy with no confirmed peer must not allocate
    state — `_pair_moves` is keyed by pair_id, not proxy_id."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.ingest_proxy_lines(_PROXY_A, [
        "position startpos moves e2e4",
    ])
    assert orch._pair_moves == {}
