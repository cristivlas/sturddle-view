"""Pair confirmation and dissolution driven by fastchess stdout
``Started game N`` / ``Finished game N`` markers.

See `docs/tournament-spec.md` § "Pair lifecycle: confirmation and
dissolution" for the design.
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
_PROXY_C  = "proxy-c"


class _FakeRunner:
    binary_path = "/fake/fastchess"
    def is_running(self) -> bool: return False
    async def start(self, spec: RunSpec, on_event) -> None: pass  # noqa: ARG002
    async def stop(self) -> None: pass


@pytest.fixture
def orch(tmp_path):
    store = TournamentStore(tmp_path / "tournaments")
    o = Orchestrator(store, _FakeRunner())
    # Active id is checked by `game_finished` / `proxy_unpaired` payloads;
    # set it to a stable value so emitted events have the field populated.
    o._active_id = "tournament-x"
    return o


@pytest.fixture
def emitted(orch):
    """Capture every event the orchestrator broadcasts."""
    events: list[tuple[str, dict]] = []
    async def cb(kind: str, payload: dict) -> None:
        events.append((kind, payload))
    orch.set_broadcast(cb)
    return events


def _events_of(emitted, kind: str) -> list[dict]:
    return [p for k, p in emitted if k == kind]


async def _confirm_pair(orch, pid_white: str, pid_black: str) -> None:
    """Drive the minimal UCI sequence that confirms a (white, black) pair."""
    await orch.ingest_proxy_lines(pid_white, [
        "position startpos",
        "bestmove e2e4",
    ])
    await orch.ingest_proxy_lines(pid_black, [
        "position startpos moves e2e4",
    ])


# ---------------------------------------------------------------------------
# Stdout regex parsing
# ---------------------------------------------------------------------------


def test_started_line_parsed(orch):
    out = orch._on_runner_log_line(
        "Started game 12 (Engine A vs Engine B)"
    )
    assert out == ("started", {"n": 12, "white": "Engine A", "black": "Engine B"})


def test_started_line_parsed_with_total(orch):
    """Real fastchess output includes ` of M` between N and the
    engines — e.g. `Started game 2045 of 4096 (...)`."""
    out = orch._on_runner_log_line(
        "Started game 2045 of 4096 (Sturddle 2.5.1 vs Sturddle 2.5.0)"
    )
    assert out == ("started", {
        "n": 2045, "white": "Sturddle 2.5.1", "black": "Sturddle 2.5.0",
    })


def test_finished_line_parsed_with_termination(orch):
    out = orch._on_runner_log_line(
        "Finished game 12 (Engine A vs Engine B): 1-0 {checkmate}"
    )
    assert out == ("finished", {
        "n": 12, "white": "Engine A", "black": "Engine B",
        "result": "1-0", "termination": "checkmate",
    })


def test_finished_line_parsed_with_long_termination(orch):
    """Real fastchess termination strings are sentences, not single
    words — e.g. `Black wins by adjudication`, `Draw by 3-fold repetition`."""
    out = orch._on_runner_log_line(
        "Finished game 2046 (Sturddle 2.5.0 vs Sturddle 2.5.1): "
        "0-1 {Black wins by adjudication}"
    )
    assert out == ("finished", {
        "n": 2046, "white": "Sturddle 2.5.0", "black": "Sturddle 2.5.1",
        "result": "0-1", "termination": "Black wins by adjudication",
    })


def test_finished_line_parsed_without_termination(orch):
    out = orch._on_runner_log_line(
        "Finished game 7 (A vs B): 1/2-1/2"
    )
    assert out == ("finished", {
        "n": 7, "white": "A", "black": "B",
        "result": "1/2-1/2", "termination": None,
    })


def test_unrelated_log_line_returns_none(orch):
    assert orch._on_runner_log_line("some random fastchess noise") is None


# ---------------------------------------------------------------------------
# Game-N stamping at confirmation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirmation_stamps_game_n_from_started_queue(orch, emitted):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    # fastchess: Started game 42 (A vs B).
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 42 ({_ENGINE_A} vs {_ENGINE_B})"})

    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    paired = _events_of(emitted, "proxy_paired")
    assert len(paired) == 1
    pair_id = paired[0]["pair_id"]
    assert pair_id  # non-empty UUID
    assert orch._pair_game_n[pair_id] == 42
    assert orch._game_n_pair[42] == pair_id
    # Started entry consumed by the FIFO match.
    assert not orch._started_games


@pytest.mark.asyncio
async def test_confirmation_before_started_is_late_stamped(orch, emitted):
    """Under load, the UCI rendezvous (HTTP) can land before fastchess's
    `Started N` stdout. The pair confirms with no game_n; when Started N
    arrives later, it must be retroactively stamped."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    # Confirm before any Started line.
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))
    assert orch._pair_game_n.get(pair_id) is None
    # Pair is queued in unstamped.
    assert any(pid == pair_id for pid, _, _ in orch._unstamped_pairs)

    # Started N arrives.
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 77 ({_ENGINE_A} vs {_ENGINE_B})"})

    assert orch._pair_game_n[pair_id] == 77
    assert orch._game_n_pair[77] == pair_id
    assert not orch._unstamped_pairs

    # Finished now resolves correctly.
    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 77 ({_ENGINE_A} vs {_ENGINE_B}): 1-0 {{checkmate}}"})
    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["game_n"] == 77


@pytest.mark.asyncio
async def test_started_queue_fifo_matches_by_color_pair(orch):
    """Two `Started` lines with the same (white, black) match
    confirmations FIFO."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch.proxy_session_started(_PROXY_C, _ENGINE_A)

    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 1 ({_ENGINE_A} vs {_ENGINE_B})"})
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 3 ({_ENGINE_A} vs {_ENGINE_B})"})

    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_1 = next(iter(orch._pair_game_n))
    assert orch._pair_game_n[pair_1] == 1


# ---------------------------------------------------------------------------
# Dissolution via Finished N
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_payload_orientation_a_is_white_b_is_black(orch, emitted):
    """`proxy_paired` and `game_finished` must always put white in `_a`
    and black in `_b`. Frozenset-iteration order is nondeterministic;
    if we let it leak through, downstream UI mislabels the board."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 11 ({_ENGINE_A} vs {_ENGINE_B})"})
    # _confirm_pair drives PROXY_A as white, PROXY_B as black.
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    paired = _events_of(emitted, "proxy_paired")[0]
    assert paired["side_a"] == "white"
    assert paired["side_b"] == "black"
    assert paired["proxy_a"] == _PROXY_A and paired["engine_a"] == _ENGINE_A
    assert paired["proxy_b"] == _PROXY_B and paired["engine_b"] == _ENGINE_B

    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 11 ({_ENGINE_A} vs {_ENGINE_B}): 1-0 {{checkmate}}"})
    finished = _events_of(emitted, "game_finished")[0]
    assert finished["proxy_a"] == _PROXY_A and finished["engine_a"] == _ENGINE_A
    assert finished["proxy_b"] == _PROXY_B and finished["engine_b"] == _ENGINE_B


@pytest.mark.asyncio
async def test_active_pairings_orientation(orch):
    """REST snapshot must also use white-in-_a orientation so a
    late-attaching workspace doesn't disagree with live-event payloads."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    snapshot = orch.active_pairings()
    assert len(snapshot) == 1
    entry = snapshot[0]
    assert entry["side_a"] == "white"
    assert entry["side_b"] == "black"
    assert entry["proxy_a"] == _PROXY_A and entry["engine_a"] == _ENGINE_A
    assert entry["proxy_b"] == _PROXY_B and entry["engine_b"] == _ENGINE_B


@pytest.mark.asyncio
async def test_finished_emits_game_finished_with_result(orch, emitted):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 5 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 5 ({_ENGINE_A} vs {_ENGINE_B}): 1-0 {{checkmate}}"})

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    payload = finished[0]
    assert payload["tournament_id"] == "tournament-x"
    assert payload["game_n"] == 5
    assert payload["result"] == "1-0"
    assert payload["termination"] == "checkmate"
    assert {payload["engine_a"], payload["engine_b"]} == {_ENGINE_A, _ENGINE_B}
    # `proxy_unpaired` is still emitted alongside (debug signal).
    assert len(_events_of(emitted, "proxy_unpaired")) == 1


@pytest.mark.asyncio
async def test_finished_drops_pair_state(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 1 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))

    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 1 ({_ENGINE_A} vs {_ENGINE_B}): 0-1"})

    assert pair_id not in orch._pair_proxies
    assert pair_id not in orch._pair_ids.values()
    assert pair_id not in orch._pair_game_n
    assert 1 not in orch._game_n_pair
    assert _PROXY_A not in orch._confirmed_pairs
    assert _PROXY_B not in orch._confirmed_pairs


@pytest.mark.asyncio
async def test_finished_for_unknown_n_is_dropped(orch, emitted):
    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 999 ({_ENGINE_A} vs {_ENGINE_B}): 1-0"})
    assert _events_of(emitted, "game_finished") == []
    assert _events_of(emitted, "proxy_unpaired") == []


@pytest.mark.asyncio
async def test_finished_sends_ws_sentinel_with_result(orch):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 9 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))
    q = orch.subscribe_to_game(pair_id)
    # Drain any snapshot replay frames first.
    while not q.empty():
        q.get_nowait()

    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 9 ({_ENGINE_A} vs {_ENGINE_B}): 1/2-1/2 {{adjudication}}"})

    msg = q.get_nowait()
    assert msg["ended"] is True
    assert msg["result"] == "1/2-1/2"
    assert msg["termination"] == "adjudication"


# ---------------------------------------------------------------------------
# Deferred dissolution: ucinewgame / proxy_session_ended don't fire game_finished
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ucinewgame_defers_dissolution(orch, emitted):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 3 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))

    # White's next round begins — UCI side ends the game from its POV.
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])

    # No terminal events yet. Pair is pending.
    assert _events_of(emitted, "game_finished") == []
    assert _events_of(emitted, "proxy_unpaired") == []
    assert pair_id in orch._pending_dissolve
    assert pair_id in orch._pair_proxies  # still addressable by Finished N
    # Linkage freed so surviving proxy can re-pair.
    assert _PROXY_A not in orch._confirmed_pairs
    assert _PROXY_B not in orch._confirmed_pairs

    # Finished line drives the actual dissolution.
    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 3 ({_ENGINE_A} vs {_ENGINE_B}): 1-0 {{checkmate}}"})

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["result"] == "1-0"
    assert pair_id not in orch._pair_proxies


@pytest.mark.asyncio
async def test_proxy_session_ended_defers_dissolution(orch, emitted):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 8 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    pair_id = next(iter(orch._pair_proxies))

    await orch.proxy_session_ended(_PROXY_A)

    # proxy_ended fires (per-proxy lifecycle), but no game terminal yet.
    assert len(_events_of(emitted, "proxy_ended")) == 1
    assert _events_of(emitted, "game_finished") == []
    assert pair_id in orch._pending_dissolve

    await orch._handle_runner_log({"stream": "out",
        "line": f"Finished game 8 ({_ENGINE_A} vs {_ENGINE_B}): 0-1 {{disconnect}}"})

    assert len(_events_of(emitted, "game_finished")) == 1


@pytest.mark.asyncio
async def test_surviving_proxy_can_repair_while_pending(orch):
    """When a peer dies mid-game, the surviving proxy must be free to
    confirm a fresh pair on its next ucinewgame."""
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch.proxy_session_started(_PROXY_C, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 1 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)
    old_pair_id = next(iter(orch._pair_proxies))

    # Peer B dies mid-game.
    await orch.proxy_session_ended(_PROXY_B)
    assert old_pair_id in orch._pending_dissolve

    # A starts a fresh game; C is the new opponent.
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 2 ({_ENGINE_A} vs {_ENGINE_B})"})
    await orch.ingest_proxy_lines(_PROXY_A, ["ucinewgame"])
    await _confirm_pair(orch, _PROXY_A, _PROXY_C)

    # New pair confirmed; A and C are now linked.
    assert orch._confirmed_pairs.get(_PROXY_A) == _PROXY_C
    assert orch._confirmed_pairs.get(_PROXY_C) == _PROXY_A
    # Two pair_ids alive — the pending old one and the new one.
    assert len(orch._pair_proxies) == 2


# ---------------------------------------------------------------------------
# Force-dissolve on terminal runner event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_engine_name_pair_rejected_as_phantom(orch, emitted):
    """Book-line collisions can leave two same-engine proxies (one
    white, one black, from different actual slots) sharing a FEN.
    Pair detection must reject them — they aren't playing each other.
    See spec § "Self-play (deferred)" for when this rule lifts."""
    # Both proxies report the same engine name (Sturddle 2.5.0 vs
    # itself, only because of book-line collision in a 2.5.0/2.5.1
    # tournament — not legitimate self-play).
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_A)
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    assert _events_of(emitted, "proxy_paired") == []
    assert not orch._pair_proxies
    assert _PROXY_A not in orch._confirmed_pairs


@pytest.mark.asyncio
async def test_force_dissolve_drains_pending_pairs(orch, emitted):
    await orch.proxy_session_started(_PROXY_A, _ENGINE_A)
    await orch.proxy_session_started(_PROXY_B, _ENGINE_B)
    await orch._handle_runner_log({"stream": "out",
        "line": f"Started game 7 ({_ENGINE_A} vs {_ENGINE_B})"})
    await _confirm_pair(orch, _PROXY_A, _PROXY_B)

    await orch._force_dissolve_pending()

    finished = _events_of(emitted, "game_finished")
    assert len(finished) == 1
    assert finished[0]["result"] == "*"
    assert finished[0]["termination"] == "unknown"
    assert not orch._pair_proxies
