"""Turn-start eviction: at the start of every Ollama AI turn we ask
the daemon what's loaded via /api/ps and evict anything that isn't
the model we're about to use. This is the only eviction path left --
settings PUTs no longer touch VRAM."""
from __future__ import annotations

import pytest

from sturddle_view.api._ai_kick import _evict_stale_ollama_models
from sturddle_view.llm.ollama import OllamaProvider


@pytest.fixture
def _track_calls(monkeypatch):
    """Replace list_loaded_models and evict_model with recording fakes.
    The fixture yields (set_loaded, evicted) so tests script the daemon's
    response and assert what got evicted."""
    state: dict = {"loaded": [], "evicted": []}

    async def _fake_list(self):
        return list(state["loaded"])

    async def _fake_evict(self, model):
        state["evicted"].append(model)

    monkeypatch.setattr(OllamaProvider, "list_loaded_models", _fake_list)
    monkeypatch.setattr(OllamaProvider, "evict_model", _fake_evict)
    return state


@pytest.mark.asyncio
async def test_evicts_stale_models_when_different_from_target(_track_calls):
    _track_calls["loaded"] = ["old:latest"]
    await _evict_stale_ollama_models("http://fake", "new:latest")
    assert _track_calls["evicted"] == ["old:latest"]


@pytest.mark.asyncio
async def test_no_evict_when_target_is_already_loaded(_track_calls):
    _track_calls["loaded"] = ["same:latest"]
    await _evict_stale_ollama_models("http://fake", "same:latest")
    assert _track_calls["evicted"] == []


@pytest.mark.asyncio
async def test_no_evict_when_nothing_loaded(_track_calls):
    _track_calls["loaded"] = []
    await _evict_stale_ollama_models("http://fake", "new:latest")
    assert _track_calls["evicted"] == []


@pytest.mark.asyncio
async def test_evicts_all_except_target_when_multiple_loaded(_track_calls):
    _track_calls["loaded"] = ["a:1", "b:2", "target:3"]
    await _evict_stale_ollama_models("http://fake", "target:3")
    assert sorted(_track_calls["evicted"]) == ["a:1", "b:2"]


@pytest.mark.asyncio
async def test_empty_target_skips_without_calling_daemon(monkeypatch):
    # Ollama enabled but no model picked yet -- we must NOT enumerate
    # loaded models (everything would mismatch the empty target and
    # get evicted for nothing). factory() will fail downstream anyway.
    calls = {"list": 0, "evict": 0}

    async def _fake_list(self):
        calls["list"] += 1
        return []

    async def _fake_evict(self, model):
        calls["evict"] += 1

    monkeypatch.setattr(OllamaProvider, "list_loaded_models", _fake_list)
    monkeypatch.setattr(OllamaProvider, "evict_model", _fake_evict)
    await _evict_stale_ollama_models("http://fake", "")
    assert calls == {"list": 0, "evict": 0}


@pytest.mark.asyncio
async def test_swallows_list_failure(monkeypatch):
    # /api/ps absent on older builds, or daemon down. Must not raise --
    # the turn should still proceed; worst case is the user sees the
    # daemon's own "resource limits" error when the new model loads.
    async def _raise(self):
        raise RuntimeError("ollama /api/ps returned 404")

    monkeypatch.setattr(OllamaProvider, "list_loaded_models", _raise)
    # Should not raise.
    await _evict_stale_ollama_models("http://fake", "new:latest")


@pytest.mark.asyncio
async def test_continues_after_individual_evict_failure(monkeypatch):
    # One model refuses to evict; the next one must still be tried.
    state = {"evicted": []}

    async def _fake_list(self):
        return ["bad:1", "good:2"]

    async def _fake_evict(self, model):
        if model == "bad:1":
            raise RuntimeError("evict failed")
        state["evicted"].append(model)

    monkeypatch.setattr(OllamaProvider, "list_loaded_models", _fake_list)
    monkeypatch.setattr(OllamaProvider, "evict_model", _fake_evict)

    await _evict_stale_ollama_models("http://fake", "target:3")
    assert state["evicted"] == ["good:2"]
