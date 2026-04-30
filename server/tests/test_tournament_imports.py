"""Slice 0 smoke test: the tournament package decomposes cleanly into its
module layout (store / runner / fastchess / orchestrator / pgn_stats /
proxy) and all classes are importable. No behavior is exercised here;
each module's stub methods raise ``NotImplementedError`` until later
slices land."""
from __future__ import annotations

import pytest


def test_package_imports():
    from sturddle_view.tournament import fastchess, orchestrator, pgn_stats, proxy, runner, store

    assert all(m is not None for m in (fastchess, orchestrator, pgn_stats, proxy, runner, store))


def test_classes_importable():
    from sturddle_view.tournament.fastchess import FastchessRunner
    from sturddle_view.tournament.orchestrator import Orchestrator
    from sturddle_view.tournament.runner import Runner
    from sturddle_view.tournament.store import TournamentStore

    assert all(c is not None for c in (FastchessRunner, Orchestrator, Runner, TournamentStore))


def test_remaining_stubs_raise_not_implemented():
    """As later slices land, methods drop from this list. Keep the test
    honest by removing the assertion when the corresponding slice ships."""
    from pathlib import Path

    from sturddle_view.tournament.fastchess import FastchessRunner
    from sturddle_view.tournament.orchestrator import Orchestrator
    from sturddle_view.tournament.pgn_stats import compute_sprt, compute_standings
    from sturddle_view.tournament.store import TournamentStore

    store = TournamentStore(Path("/tmp/nonexistent"))
    runner = FastchessRunner("/usr/bin/false")
    orch = Orchestrator(store, runner)

    # pgn_stats: Slice 2
    with pytest.raises(NotImplementedError):
        compute_standings(Path("/dev/null"))
    with pytest.raises(NotImplementedError):
        compute_sprt(Path("/dev/null"), {})

    # orchestrator: Slice 4
    with pytest.raises(NotImplementedError):
        orch.reconcile_on_startup()

    # runner: Slice 3
    assert runner.is_running() is False
