"""Content-Security-Policy: eval stays refused unless the app opts in
(the Linux desktop app, for pywebview's GTK bridge)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry

UNSAFE_EVAL = "'unsafe-eval'"


@pytest.mark.parametrize("allow_eval", [False, True])
def test_csp_allows_eval_only_when_opted_in(tmp_path, allow_eval):
    settings = Settings(token="t", auth_disabled=True)
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry, allow_eval=allow_eval)
    with TestClient(app) as client:
        csp = client.get("/ui/").headers["content-security-policy"]
    assert csp.startswith("default-src 'self'")
    assert (UNSAFE_EVAL in csp) is allow_eval
