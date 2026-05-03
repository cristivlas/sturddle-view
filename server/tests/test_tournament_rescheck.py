"""Resource sanity checks (``rescheck.py``) + the rescheck endpoint."""
from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.tournament.fastchess import FastchessRunner
from sturddle_view.tournament.rescheck import (
    ENGINE_OVERHEAD_MB,
    HostSpecs,
    RescheckError,
    check,
)


# ---------------------------------------------------------------------------
# Pure module
# ---------------------------------------------------------------------------


_HOST_8C_8GB = HostSpecs(logical_cores=8, physical_cores=4, total_ram_mb=8192)


def test_check_passes_under_budget():
    warnings = check(
        parallel=2, max_threads=2, max_hash_mb=128, specs=_HOST_8C_8GB,
    )
    assert warnings == []


def test_check_blocks_cpu_oversubscription():
    with pytest.raises(RescheckError) as ei:
        check(parallel=9, max_threads=1, max_hash_mb=16, specs=_HOST_8C_8GB)
    assert ei.value.reason == "oversubscribed"
    assert ei.value.details["cpu_load"] == 9
    assert ei.value.details["logical_cores"] == 8


def test_check_ponder_doubles_cpu_load():
    # 5 * 2 (ponder) * 1 = 10 > 8 logical → blocks
    with pytest.raises(RescheckError) as ei:
        check(
            parallel=5, max_threads=1, max_hash_mb=16, ponder=True,
            specs=_HOST_8C_8GB,
        )
    assert ei.value.reason == "oversubscribed"
    assert ei.value.details["cpu_load"] == 10


def test_check_allow_oversubscribe_silences_cpu_to_warning():
    warnings = check(
        parallel=9, max_threads=1, max_hash_mb=16, allow_oversubscribe=True,
        specs=_HOST_8C_8GB,
    )
    assert any(w["reason"] == "oversubscribed" for w in warnings)


def test_check_blocks_affinity_over_physical():
    with pytest.raises(RescheckError) as ei:
        check(
            parallel=5, max_threads=1, max_hash_mb=16, pin_affinity=True,
            specs=_HOST_8C_8GB,
        )
    assert ei.value.reason == "affinity_exceeds_physical"


def test_check_affinity_block_not_silenced_by_oversubscribe():
    # Affinity is a correctness gate — flag must not bypass.
    with pytest.raises(RescheckError) as ei:
        check(
            parallel=5, max_threads=1, max_hash_mb=16,
            pin_affinity=True, allow_oversubscribe=True,
            specs=_HOST_8C_8GB,
        )
    assert ei.value.reason == "affinity_exceeds_physical"


def test_check_blocks_ram_when_over_budget():
    # 4 * 2 * (4096 + overhead) = 34816 MB > 0.75 * 8192 = 6144
    with pytest.raises(RescheckError) as ei:
        check(parallel=4, max_threads=1, max_hash_mb=4096, specs=_HOST_8C_8GB)
    assert ei.value.reason == "insufficient_ram"
    assert ei.value.details["ram_load_mb"] == 4 * 2 * (4096 + ENGINE_OVERHEAD_MB)


def test_check_allow_oversubscribe_silences_ram_to_warning():
    warnings = check(
        parallel=4, max_threads=1, max_hash_mb=4096, allow_oversubscribe=True,
        specs=_HOST_8C_8GB,
    )
    assert any(w["reason"] == "insufficient_ram" for w in warnings)


# ---------------------------------------------------------------------------
# /api/tournaments/rescheck endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    s = Settings(auth_disabled=True)
    s.tournament_root = str(tmp_path / "tournaments")
    s.tournament_fastchess_path = sys.executable
    monkeypatch.setattr(
        FastchessRunner, "detect_binary",
        staticmethod(lambda configured: configured),
    )
    app = create_app(settings=s)
    with TestClient(app) as c:
        yield c


def _patch_specs(monkeypatch, *, logical=8, physical=4, total_ram_mb=8192):
    monkeypatch.setattr(
        "sturddle_view.tournament.rescheck.host_specs",
        lambda: HostSpecs(logical, physical, total_ram_mb),
    )


def test_rescheck_endpoint_ok(client, monkeypatch):
    _patch_specs(monkeypatch)
    r = client.post("/api/tournaments/rescheck", json={
        "parallel": 2, "max_threads": 2, "max_hash_mb": 128,
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "warnings": []}


def test_rescheck_endpoint_400_on_oversubscribe(client, monkeypatch):
    _patch_specs(monkeypatch)
    r = client.post("/api/tournaments/rescheck", json={
        "parallel": 9, "max_threads": 1, "max_hash_mb": 16,
    })
    assert r.status_code == 400, r.text
    body = r.json()["detail"]
    assert body["reason"] == "oversubscribed"
    assert body["cpu_load"] == 9
    assert body["logical_cores"] == 8


def test_rescheck_endpoint_returns_warnings_when_overridden(client, monkeypatch):
    _patch_specs(monkeypatch)
    r = client.post("/api/tournaments/rescheck", json={
        "parallel": 9, "max_threads": 1, "max_hash_mb": 16,
        "allow_oversubscribe": True,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert any(w["reason"] == "oversubscribed" for w in body["warnings"])
