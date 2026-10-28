"""E2E: Settings > Analysis shows the Lines input beside the Engine picker
only for an engine whose probe found MultiPV -- including the engine
pre-selected when the tab opens, not just after a picker change.

Registry entries are seeded as already probed (uci_name + multipv_max), so
the listing spawns nothing. Skipped if Playwright is missing.
"""
from __future__ import annotations

import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import (  # noqa: E402
    REGISTRY_FILE,
    assert_no_page_errors,
    e2e_env,
    make_fake_uci,
    run_uvicorn_subprocess,
    wait_perspective_ready,
    watch_page_errors,
)

MULTIPV_ENGINE = "engine-multipv"
SINGLE_PV_ENGINE = "engine-single"
ENGINE_MULTIPV_MAX = 500
LINES = ".ai-analysis-lines"
ENGINE_LABEL = "Engine"
OPEN_ANALYSIS_TAB = (
    "window.dispatchEvent(new CustomEvent('sturddle:open-settings', { detail: { tab: 'analysis' } }))"
)
# The model row turns into the engine picker (label "Engine") in the same
# synchronous call that settles Lines' visibility.
ENGINE_PICKER_READY = f"""() => [...document.querySelectorAll('.ai-model-field label')]
  .some((l) => l.textContent === '{ENGINE_LABEL}')"""
LINES_SHOWN = f"() => getComputedStyle(document.querySelector('{LINES}')).display !== 'none'"


def _seed(tmp_path, selected: str) -> dict:
    reg = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    ids = {}
    for name, multipv_max in ((MULTIPV_ENGINE, ENGINE_MULTIPV_MAX), (SINGLE_PV_ENGINE, 0)):
        ids[name] = reg.add(
            name=name, path=make_fake_uci(tmp_path, name), uci_name=name, multipv_max=multipv_max,
        ).id
    reg.select(ids[selected])
    return e2e_env(tmp_path)


async def _open_analysis_tab(page, base):
    await page.goto(base + "/")
    await page.wait_for_selector("#play-perspective")
    await wait_perspective_ready(page)
    await page.evaluate(OPEN_ANALYSIS_TAB)
    await page.wait_for_function(ENGINE_PICKER_READY)


@pytest.mark.asyncio
@pytest.mark.parametrize(("selected", "shown"), [(MULTIPV_ENGINE, True), (SINGLE_PV_ENGINE, False)])
async def test_lines_follows_preselected_engine_on_open(tmp_path, make_page, selected, shown):
    with run_uvicorn_subprocess(env_overrides=_seed(tmp_path, selected)) as base:
        _ctx, page = await make_page()
        errors = watch_page_errors(page)
        await _open_analysis_tab(page, base)
        assert await page.evaluate(LINES_SHOWN) is shown
        assert_no_page_errors(errors)
