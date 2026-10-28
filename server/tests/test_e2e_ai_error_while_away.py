"""E2E regression: an AI turn that fails while Play is unmounted still toasts.

Leaving Play mid-analysis drops its bus listener, so the failed turn's
done/error event reaches no handler. The server keeps that event in the
replay buffer until a client has shown it: returning to Play rehydrates it
and raises the error toast. The showing client acknowledges it, so a reload
afterwards does not toast the same error again.

The provider is a local Ollama stand-in answering every request with 503.

Skipped if Playwright is missing.
"""
from __future__ import annotations

import json
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import httpx
import pytest

pytest.importorskip("playwright.async_api")
pytestmark = pytest.mark.e2e

from sturddle_view.engines import EngineRegistry  # noqa: E402

from .conftest import REGISTRY_FILE, e2e_env, run_uvicorn_subprocess  # noqa: E402


PLAY_PERSP = "#play-perspective"
PLAY_NAV_BTN = "#perspective-nav button[data-perspective='play']"
OTHER_NAV_BTN = "#perspective-nav button[data-perspective='engines']"
_TOAST = "#toast-stack .toast-danger"
REPLAY_PATH = "/game/analysis/replay"
AI_INFO_KIND = "ai_info"
# Mirrors APP_EVT.AI_REHYDRATED in web/app/app-events.js.
AI_REHYDRATED_EVT = "sturddle:ai-rehydrated"
VIEWPORT = {"width": 1600, "height": 1000}
LOCALHOST = "127.0.0.1"
LLM_ERROR = "model is overloaded"

_PGN = (
    '[Event "?"]\n[Site "?"]\n[Date "????.??.??"]\n[Round "?"]\n'
    '[White "W"]\n[Black "B"]\n[Result "*"]\n\n'
    '1. e4 e5 2. Nf3 Nc6 *\n'
)


class _OverloadedLlm(BaseHTTPRequestHandler):
    """Every request fails with 503, like a provider under load."""

    def _reply(self) -> None:
        # Drain the request body first: closing on unread data resets the
        # connection, and the client would see that instead of the 503.
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"error": LLM_ERROR}).encode()
        self.send_response(HTTPStatus.SERVICE_UNAVAILABLE)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _reply

    def log_message(self, *_args) -> None:
        pass


@pytest.fixture
def llm_url():
    srv = ThreadingHTTPServer((LOCALHOST, 0), _OverloadedLlm)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://{LOCALHOST}:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def server(tmp_path, llm_url):
    seed = EngineRegistry(path=tmp_path / REGISTRY_FILE)
    e = seed.add(name="MyEngine", path="/nonexistent/engine")
    seed.select(e.id)
    with run_uvicorn_subprocess(env_overrides=e2e_env(tmp_path)) as base:
        httpx.put(f"{base}/settings", json={
            "ai_enabled": True, "ai_provider": "ollama", "ai_base_url": llm_url,
        }).raise_for_status()
        httpx.post(f"{base}/game/import",
                   json={"text": _PGN, "format": "pgn"}).raise_for_status()
        yield base


def _start_analysis(base) -> None:
    httpx.post(f"{base}/game/analysis/start").raise_for_status()


async def _count_rehydrates(page) -> None:
    """Count AI_REHYDRATED from document start (each Play mount fires one)."""
    await page.add_init_script(f"""
        window.__aiRehydrated = 0;
        window.addEventListener('{AI_REHYDRATED_EVT}', () => {{ window.__aiRehydrated += 1; }});
    """)


async def _await_rehydrates(page, n: int) -> None:
    await page.wait_for_function(f"() => window.__aiRehydrated >= {n}")


def _is_ai_error_frame(frame) -> bool:
    """The failed turn's done/error event on the page's socket -- whether or
    not anything on the page handles it."""
    try:
        ev = json.loads(frame)
    except ValueError:
        return False
    p = ev.get("payload") or {}
    return ev.get("kind") == AI_INFO_KIND and bool(p.get("done") and p.get("error"))


def _replay_acked(resp) -> bool:
    return urlparse(resp.url).path == REPLAY_PATH and resp.request.method == "DELETE"


@pytest.mark.asyncio
async def test_error_while_away_toasts_on_return(server, make_page):
    base = server
    _ctx, page = await make_page(viewport=VIEWPORT)
    await _count_rehydrates(page)
    async with page.expect_websocket() as ws_info:
        await page.goto(base + "/")
    ws = await ws_info.value
    await _await_rehydrates(page, 1)

    await page.click(OTHER_NAV_BTN)
    await page.wait_for_selector(PLAY_PERSP, state="detached")
    async with ws.expect_event("framereceived", predicate=_is_ai_error_frame):
        _start_analysis(base)

    await page.click(PLAY_NAV_BTN)
    await _await_rehydrates(page, 2)
    toast = page.locator(_TOAST)
    assert await toast.count() == 1, "failed turn left no error toast on return"
    assert LLM_ERROR in (await toast.text_content() or "")


@pytest.mark.asyncio
async def test_shown_error_does_not_toast_again_on_reload(server, make_page):
    base = server
    _ctx, page = await make_page(viewport=VIEWPORT)
    await _count_rehydrates(page)
    await page.goto(base + "/")
    await _await_rehydrates(page, 1)

    async with page.expect_response(_replay_acked):
        _start_analysis(base)
    assert await page.locator(_TOAST).count() == 1, "live error did not toast"

    await page.reload()
    await _await_rehydrates(page, 1)
    assert await page.locator(_TOAST).count() == 0, "acked error toasted again"
