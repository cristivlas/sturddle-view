"""Opening list endpoint and opening-aware /game/import behavior."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sturddle_view.app import create_app
from sturddle_view.config import Settings
from sturddle_view.engines import EngineRegistry
from sturddle_view.play.human_vs_engine import HumanVsEngine
from sturddle_view.recent_imports import RecentImports

CARO_KANN_PGN = "1. e4 c6 2. d4 d5"
CARO_KANN = {"eco": "B12", "name": "Caro-Kann Defense: Advance Variation"}
ENGINE_NAME = "MyEngine"


def _make_app(tmp_path, human_side="white"):
    settings = Settings(token="test-token")
    settings.human_side = human_side
    registry = EngineRegistry(path=tmp_path / "engines.json")
    e = registry.add(name=ENGINE_NAME, path=str(tmp_path / "fake-engine"))
    registry.select(e.id)
    app = create_app(settings=settings, engine_registry=registry)
    app.state.recent_imports = RecentImports.load(root=tmp_path / "imports", cap=5)
    app.state.hve = HumanVsEngine(
        engine_path=str(tmp_path / "fake-engine"),
        bus=app.state.event_bus,
        openings=app.state.openings,
        settings=app.state.settings,
    )
    return app


def _client(app):
    c = TestClient(app)
    c.headers["Authorization"] = "Bearer test-token"
    return c


@pytest.fixture
def client(tmp_path):
    with _client(_make_app(tmp_path)) as c:
        yield c


# --- GET /openings --------------------------------------------------------

def test_openings_list_returns_rows(client):
    r = client.get("/openings")
    assert r.status_code == 200, r.text
    rows = r.json()["results"]
    assert len(rows) > 0
    row = rows[0]
    assert set(row) == {"eco", "name", "pgn", "ply"}


def test_openings_list_requires_auth(tmp_path):
    app = _make_app(tmp_path)
    with TestClient(app) as c:  # no Authorization header
        assert c.get("/openings").status_code == 401


# --- /game/import with opening identity -----------------------------------

def test_import_opening_labels_sides_and_opening(client):
    r = client.post(
        "/game/import",
        json={"format": "pgn", "text": CARO_KANN_PGN, "opening": CARO_KANN},
    )
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert summary["opening"] == f"{CARO_KANN['eco']} {CARO_KANN['name']}"
    # human_side=white -> player on White, engine on Black.
    assert summary["white"] == "Human"
    assert summary["black"] == ENGINE_NAME


def test_import_opening_black_side(tmp_path):
    with _client(_make_app(tmp_path, human_side="black")) as c:
        r = c.post(
            "/game/import",
            json={"format": "pgn", "text": CARO_KANN_PGN, "opening": CARO_KANN},
        )
        summary = r.json()["summary"]
        assert summary["white"] == ENGINE_NAME
        assert summary["black"] == "Human"


def test_import_opening_random_side_assigns_consistently(tmp_path):
    """random still produces a concrete player-vs-engine pairing (the import
    IS the side assignment); the two names are player + engine, some order."""
    with _client(_make_app(tmp_path, human_side="random")) as c:
        r = c.post(
            "/game/import",
            json={"format": "pgn", "text": CARO_KANN_PGN, "opening": CARO_KANN},
        )
        summary = r.json()["summary"]
        assert {summary["white"], summary["black"]} == {"Human", ENGINE_NAME}


def test_import_opening_non_dict_is_400(client):
    r = client.post(
        "/game/import", json={"format": "pgn", "text": CARO_KANN_PGN, "opening": "nope"},
    )
    assert r.status_code == 400


def test_import_opening_non_string_fields_is_400(client):
    r = client.post(
        "/game/import",
        json={"format": "pgn", "text": CARO_KANN_PGN,
              "opening": {"eco": "B12", "name": {"x": 1}}},
    )
    assert r.status_code == 400


def test_import_blank_opening_name_does_not_relabel(client):
    """A blank opening name leaves the parsed sides untouched (no spurious
    player/engine relabel, no opening label)."""
    r = client.post(
        "/game/import",
        json={"format": "pgn", "text": CARO_KANN_PGN,
              "opening": {"eco": "", "name": "   "}},
    )
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert "opening" not in summary or not summary["opening"]
    # Parsed PGN had no White/Black headers -> sides stay unset.
    assert not summary.get("white")
    assert not summary.get("black")


def test_import_without_opening_unchanged(client):
    """A plain PGN import (no opening field) is unaffected."""
    r = client.post("/game/import", json={"format": "pgn", "text": CARO_KANN_PGN})
    assert r.status_code == 200, r.text
    assert "opening" not in r.json()["summary"]
