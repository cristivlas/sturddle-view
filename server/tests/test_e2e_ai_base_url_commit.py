"""E2E regression: the Ollama Base URL commits on Enter/blur, not per keystroke.

The bug persisted the URL on a debounced input, and every persist re-fetched
/settings/ai/models (server -> Ollama /api/tags + /api/show per model). A
typing pause on a partial URL fired a fetch that failed and toasted.

Fetches are logged in-page (api() calls fetch synchronously), and the fake
clock steps past the debounce, so no real waits are involved.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from .conftest import e2e_env, run_uvicorn_subprocess, wait_perspective_ready  # noqa: E402


MODELS_PATH = "/settings/ai/models"
SETTINGS_PATH = "/settings"
BASE_URL_KEY = "ai_base_url"
TYPED_URL = "http://localhost:11434"
# Well past the old 400ms debounce: under the bug its persist fires here.
DEBOUNCE_ADVANCE_MS = 1000
WAIT_MS = 5000

_FETCH_LOG_SCRIPT = """
window.__fetchLog = [];
const origFetch = window.fetch;
window.fetch = function (input, init) {
  const url = typeof input === "string" ? input : input.url;
  window.__fetchLog.push({
    method: (init && init.method) || "GET",
    url,
    body: (init && init.body) || "",
  });
  return origFetch.apply(this, arguments);
};
"""

_COUNTS_JS = """([modelsPath, settingsPath, key]) => {
  const log = window.__fetchLog;
  return {
    models: log.filter((e) => e.url === modelsPath).length,
    urlPuts: log.filter((e) => e.method === "PUT" && e.url === settingsPath
                          && e.body.includes(key)).map((e) => JSON.parse(e.body)[key]),
  };
}"""

_MODELS_AT_LEAST_JS = """([modelsPath, n]) =>
  window.__fetchLog.filter((e) => e.url === modelsPath).length >= n"""


@pytest.fixture
def server(tmp_path):
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        httpx.put(f"{base}{SETTINGS_PATH}",
                  json={"ai_enabled": True, "ai_provider": "ollama", BASE_URL_KEY: ""}
                  ).raise_for_status()
        yield base


async def _counts(page):
    return await page.evaluate(_COUNTS_JS, [MODELS_PATH, SETTINGS_PATH, BASE_URL_KEY])


async def _open_and_type(server, make_page):
    _ctx, page = await make_page(viewport={"width": 1200, "height": 800})
    await page.add_init_script(_FETCH_LOG_SCRIPT)
    await page.clock.install()
    # Stub the provider list so no request reaches a real Ollama.
    await page.route(f"**{MODELS_PATH}",
                     lambda route: route.fulfill(json={"models": ["m1"], "thinking": {}}))
    await page.goto(server + "/")
    await wait_perspective_ready(page)
    await page.evaluate(
        "window.dispatchEvent(new CustomEvent('sturddle:open-settings', "
        "{ detail: { tab: 'analysis' } }))"
    )
    # Mount-time fetch is the baseline.
    await page.wait_for_function(_MODELS_AT_LEAST_JS, arg=[MODELS_PATH, 1], timeout=WAIT_MS)
    url_input = page.locator(".settings-row", has=page.locator("label", has_text="Base URL")).locator("wa-input")
    await url_input.click()
    await page.keyboard.type(TYPED_URL)
    await page.clock.run_for(DEBOUNCE_ADVANCE_MS)
    counts = await _counts(page)
    assert counts == {"models": 1, "urlPuts": []}, f"typing must not persist/fetch: {counts}"
    return page


@pytest.mark.asyncio
async def test_base_url_commits_once_on_enter(server, make_page):
    page = await _open_and_type(server, make_page)
    await page.keyboard.press("Enter")
    await page.wait_for_function(_MODELS_AT_LEAST_JS, arg=[MODELS_PATH, 2], timeout=WAIT_MS)
    assert await _counts(page) == {"models": 2, "urlPuts": [TYPED_URL]}


@pytest.mark.asyncio
async def test_base_url_commits_once_on_blur(server, make_page):
    page = await _open_and_type(server, make_page)
    await page.keyboard.press("Tab")
    await page.wait_for_function(_MODELS_AT_LEAST_JS, arg=[MODELS_PATH, 2], timeout=WAIT_MS)
    assert await _counts(page) == {"models": 2, "urlPuts": [TYPED_URL]}
