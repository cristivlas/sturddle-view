"""Global engine-defaults layered onto per-engine UCI options at launch.

The actual engine.configure() call is exercised by python-chess; here
we just assert the helper that builds the override dict from Settings
matches what the launch path will apply on top of per-engine values.
"""
from __future__ import annotations

from sturddle_view.config import Settings
from sturddle_view.events import EventBus
from sturddle_view.play.human_vs_engine import HumanVsEngine


def _make_hve(**settings_kwargs) -> HumanVsEngine:
    settings = Settings(token="t", auth_disabled=True, **settings_kwargs)
    return HumanVsEngine(engine_path="/nonexistent", bus=EventBus(), settings=settings)


def test_no_settings_yields_empty():
    h = HumanVsEngine(engine_path="/nonexistent", bus=EventBus(), settings=None)
    assert h._global_engine_defaults() == {}


def test_unset_fields_yield_empty():
    h = _make_hve()
    assert h._global_engine_defaults() == {}


def test_threads_only():
    h = _make_hve(engine_default_threads=4)
    assert h._global_engine_defaults() == {"Threads": 4}


def test_all_three_uci_overrides():
    h = _make_hve(
        engine_default_threads=8,
        engine_default_hash_mb=512,
        engine_default_syzygy_path="/srv/syzygy",
    )
    assert h._global_engine_defaults() == {
        "Threads": 8,
        "Hash": 512,
        "SyzygyPath": "/srv/syzygy",
    }


def test_book_fields_excluded_from_uci():
    """Book file + plies are fastchess-only; they must not surface as
    UCI setoptions for HVE (see project_hve_book_followup memory)."""
    h = _make_hve(
        engine_default_book_path="/srv/book.epd",
        engine_default_book_plies=8,
    )
    assert h._global_engine_defaults() == {}


def test_zero_threads_treated_as_unset():
    """Zero/blank means no override even if it slipped past the API
    coercion (defence in depth)."""
    h = _make_hve(engine_default_threads=0, engine_default_hash_mb=0)
    assert h._global_engine_defaults() == {}
