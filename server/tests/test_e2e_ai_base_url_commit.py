"""E2E regression: the Ollama Base URL commits on Enter/blur, not per keystroke.

The bug persisted the URL on a debounced input, and every persist re-fetched
/settings/ai/models (server -> Ollama /api/tags + /api/show per model). A
typing pause on a partial URL fired a fetch that failed and toasted.

An invalid URL never saves: Enter/Tab shake and refocus; close or moving to
the provider select reverts.

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
INVALID_URL = "foo"
BASE_URL_LABEL = "Base URL"
PROVIDER_LABEL = "Provider"
MODEL_LABEL = "Model"
STUB_MODEL = "m1"
WA_INPUT = "wa-input"
WA_SELECT = "wa-select"
ENTER = "Enter"
TAB = "Tab"
ESCAPE = "Escape"
NOTHING_SAVED = {"models": 1, "urlPuts": []}
# Well past the old 400ms debounce: under the bug its persist fires here.
DEBOUNCE_ADVANCE_MS = 1000

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

# Latches window.__shook when any element gains the shake class, so the
# check can't miss the short-lived class.
_SHAKE_RECORDER_SCRIPT = """
window.__shook = false;
new MutationObserver((muts) => {
  if (muts.some((m) => m.target.classList.contains("field-shake"))) window.__shook = true;
}).observe(document, { attributes: true, attributeFilter: ["class"], subtree: true });
"""

_COUNTS_JS ="""([modelsPath, settingsPath, key]) => {
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


def _row_control(page, label, tag):
    return page.locator(".settings-row", has=page.locator("label", has_text=label)).locator(tag)


async def _open_and_type(server, make_page, text=TYPED_URL):
    _ctx, page = await make_page(viewport={"width": 1200, "height": 800})
    await page.add_init_script(_FETCH_LOG_SCRIPT)
    await page.add_init_script(_SHAKE_RECORDER_SCRIPT)
    await page.clock.install()
    # Stub the provider list so no request reaches a real Ollama.
    await page.route(f"**{MODELS_PATH}",
                     lambda route: route.fulfill(json={"models": [STUB_MODEL], "thinking": {}}))
    await page.goto(server + "/")
    await wait_perspective_ready(page)
    await page.evaluate(
        "window.dispatchEvent(new CustomEvent('sturddle:open-settings', "
        "{ detail: { tab: 'analysis' } }))"
    )
    # Mount-time fetch is the baseline.
    await page.wait_for_function(_MODELS_AT_LEAST_JS, arg=[MODELS_PATH, 1])
    # ...and applied: its re-render must land before the clicks below, or a
    # late one moves the dialog under a click (it then misses the select).
    model_select = await _row_control(page, MODEL_LABEL, WA_SELECT).element_handle()
    await page.wait_for_function("([el, m]) => el.value === m", arg=[model_select, STUB_MODEL])
    url_input = _row_control(page, BASE_URL_LABEL, WA_INPUT)
    await url_input.click()
    await page.keyboard.type(text)
    await page.clock.run_for(DEBOUNCE_ADVANCE_MS)
    counts = await _counts(page)
    assert counts == NOTHING_SAVED, f"typing must not persist/fetch: {counts}"
    return page, url_input


async def _shook(page) -> bool:
    return await page.evaluate("() => window.__shook")


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [ENTER, TAB])
async def test_base_url_commits_once(server, make_page, key):
    page, _url_input = await _open_and_type(server, make_page)
    await page.keyboard.press(key)
    await page.wait_for_function(_MODELS_AT_LEAST_JS, arg=[MODELS_PATH, 2])
    assert await _counts(page) == {"models": 2, "urlPuts": [TYPED_URL]}
    assert not await _shook(page)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", [ENTER, TAB])
async def test_invalid_base_url_shakes_refocuses_and_does_not_save(server, make_page, key):
    page, url_input = await _open_and_type(server, make_page, INVALID_URL)
    await page.keyboard.press(key)
    await page.wait_for_function("() => window.__shook")
    await page.wait_for_function("(el) => el === document.activeElement",
                                 arg=await url_input.element_handle())
    await page.clock.run_for(DEBOUNCE_ADVANCE_MS)
    assert await _counts(page) == NOTHING_SAVED


@pytest.mark.asyncio
async def test_invalid_base_url_not_saved_on_close(server, make_page):
    page, _url_input = await _open_and_type(server, make_page, INVALID_URL)
    await page.keyboard.press(ESCAPE)
    await page.wait_for_function("() => !document.querySelector('wa-dialog[open]')")
    assert await _counts(page) == NOTHING_SAVED
    assert httpx.get(f"{server}{SETTINGS_PATH}").json()[BASE_URL_KEY] == ""


@pytest.mark.asyncio
async def test_invalid_base_url_reverts_on_provider_click(server, make_page):
    page, url_input = await _open_and_type(server, make_page, INVALID_URL)
    await _row_control(page, PROVIDER_LABEL, WA_SELECT).click()
    await page.wait_for_function("(el) => el.value === ''", arg=await url_input.element_handle())
    assert not await _shook(page)
    assert await _counts(page) == NOTHING_SAVED
