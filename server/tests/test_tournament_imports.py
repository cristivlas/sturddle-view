"""Tournament package smoke tests: every submodule is importable and the
public classes are accessible. Behavior is exercised in the per-module
test files (``test_tournament_store``, ``test_tournament_pgn_stats``,
``test_tournament_fastchess``, ``test_tournament_orchestrator``)."""
from __future__ import annotations


def test_package_imports():
    from sturddle_view.tournament import fastchess, orchestrator, pgn_stats, proxy, runner, store

    assert all(m is not None for m in (fastchess, orchestrator, pgn_stats, proxy, runner, store))


def test_classes_importable():
    from sturddle_view.tournament.fastchess import FastchessRunner
    from sturddle_view.tournament.orchestrator import Orchestrator
    from sturddle_view.tournament.runner import Runner
    from sturddle_view.tournament.store import TournamentStore

    assert all(c is not None for c in (FastchessRunner, Orchestrator, Runner, TournamentStore))
