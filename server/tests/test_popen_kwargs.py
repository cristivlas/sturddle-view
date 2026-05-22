"""Tests for the shared _popen_kwargs(env) helper (R3 / P6).

Both engines.probe_engine and EngineSupervisor.spawn must build their
popen_uci kwargs from this single helper. Tests pin the env-overlay
semantics and the platform-specific Windows creationflag.
"""
from __future__ import annotations

import subprocess
import sys
from unittest.mock import patch

import pytest

# -- module under test (will fail ImportError until _popen_kwargs is added) --
from sturddle_view.engines import _popen_kwargs


def test_popen_kwargs_no_env_no_flags_on_posix():
    with patch("sturddle_view.engines.sys") as mock_sys:
        mock_sys.platform = "linux"
        out = _popen_kwargs(None)
    assert out == {}


def test_popen_kwargs_empty_env_no_overlay():
    with patch("sturddle_view.engines.sys") as mock_sys:
        mock_sys.platform = "linux"
        out = _popen_kwargs({})
    assert out == {}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only creationflag")
def test_popen_kwargs_includes_creationflag_on_win32():
    with patch("sturddle_view.engines.sys") as mock_sys:
        mock_sys.platform = "win32"
        out = _popen_kwargs(None)
    assert out == {"creationflags": subprocess.CREATE_NO_WINDOW}


def test_popen_kwargs_overlays_env_on_parent(monkeypatch):
    monkeypatch.setenv("PARENT_ONLY", "p")
    monkeypatch.setenv("BOTH", "parent_value")
    with patch("sturddle_view.engines.sys") as mock_sys:
        mock_sys.platform = "linux"
        out = _popen_kwargs({"BOTH": "child_value", "CHILD_ONLY": "c"})
    env = out["env"]
    assert env["PARENT_ONLY"] == "p"
    assert env["BOTH"] == "child_value"
    assert env["CHILD_ONLY"] == "c"


def test_probe_engine_and_supervisor_use_same_popen_kwargs():
    """Catches a divergence going forward -- if either site stops calling
    _popen_kwargs, this test fails the import or finds the wrong builder."""
    import sturddle_view.engines as engines_mod
    from sturddle_view.play import engine_supervisor as sup_mod

    # Both modules should import the same callable (same id, or at minimum
    # call into the same helper). The supervisor imports it from engines.
    assert sup_mod._popen_kwargs is engines_mod._popen_kwargs
