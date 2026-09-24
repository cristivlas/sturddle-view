"""env_utils: malformed or out-of-range env values fall back to the default."""
from __future__ import annotations

from pathlib import Path

import pytest

from sturddle_view.env_utils import env_bool, env_choice, env_float, env_int, env_path

_VAR = "SVTEST_ENV_UTILS"


def test_env_int_non_numeric_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "not-an-int")
    assert env_int(_VAR, 17) == 17


def test_env_float_non_numeric_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "not-a-number")
    assert env_float(_VAR, 42.0) == 42.0


def test_env_int_below_min_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "0")
    assert env_int(_VAR, 5, min_value=1) == 5


def test_env_float_below_min_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "0.01")
    assert env_float(_VAR, 3.0, min_value=0.05) == 3.0


def test_env_float_at_min_is_kept(monkeypatch):
    monkeypatch.setenv(_VAR, "0.05")
    assert env_float(_VAR, 3.0, min_value=0.05) == 0.05


@pytest.mark.parametrize("raw, expected", [("yes", True), ("OFF", False), ("", False)])
def test_env_bool_tokens(monkeypatch, raw, expected):
    monkeypatch.setenv(_VAR, raw)
    assert env_bool(_VAR, not expected) is expected


def test_env_choice_is_case_insensitive(monkeypatch):
    monkeypatch.setenv(_VAR, " Warn ")
    assert env_choice(_VAR, "info", {"info", "warn"}) == "warn"


def test_env_choice_unknown_returns_default(monkeypatch):
    monkeypatch.setenv(_VAR, "loud")
    assert env_choice(_VAR, "info", {"info", "warn"}) == "info"


def test_env_path_set_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setenv(_VAR, str(tmp_path / "x.json"))
    assert env_path(_VAR, Path("default.json")) == tmp_path / "x.json"


@pytest.mark.parametrize("raw", [None, ""])
def test_env_path_unset_or_empty_returns_default(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv(_VAR, raising=False)
    else:
        monkeypatch.setenv(_VAR, raw)
    assert env_path(_VAR, Path("default.json")) == Path("default.json")
