"""One-off stress repro for the wsproto LocalProtocolError that used to
surface during test_e2e_slot_grid teardown.

Opt-in only -- skipped unless SV_WS_STRESS=1 is set.

Run with:
    SV_WS_STRESS=1 pytest server/tests/test_ws_shutdown_repro.py -s

Background: uvicorn's WSProtocol.shutdown() sends CloseConnection(1012)
without checking conn.state. When a client cleanly closes right before
server shutdown iterates its connections set, wsproto is already CLOSED
and the send raises LocalProtocolError on the loop thread, surfaced as
PytestUnhandledThreadExceptionWarning.

The runtime fix lives in sturddle_view.app._install_uvicorn_ws_shutdown_state_guard.
This stress harness reliably reproduces the race (a few percent per
iteration without the guard, 0/N with it) and is preserved for future
troubleshooting.

Useful knobs:
    SV_WS_STRESS_ITERS         -- iteration count (default 20)
    SV_WS_STRESS_CLIENTS       -- WSs per iteration (default 12)
    SV_WS_STRESS_BYPASS_GUARD  -- remove the app guard mid-test to
                                  confirm the harness still repros
                                  against vanilla uvicorn
"""
from __future__ import annotations

import os
import socket
import threading
import time
import traceback

import pytest

if not os.environ.get("SV_WS_STRESS"):
    pytest.skip("set SV_WS_STRESS=1 to run", allow_module_level=True)

pytest.importorskip("playwright.async_api")

from sturddle_view.app import create_app  # noqa: E402
from sturddle_view.config import Settings  # noqa: E402
from sturddle_view.engines import EngineRegistry  # noqa: E402


ITERATIONS = int(os.environ.get("SV_WS_STRESS_ITERS", "20"))
CLIENTS_PER_ITER = int(os.environ.get("SV_WS_STRESS_CLIENTS", "12"))
BYPASS_GUARD = bool(os.environ.get("SV_WS_STRESS_BYPASS_GUARD"))


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(tmp_path):
    import uvicorn

    settings = Settings(token="test-token", auth_disabled=True)
    settings.pgn_dir = tmp_path / "pgn"
    registry = EngineRegistry(path=tmp_path / "engines.json")
    app = create_app(settings=settings, engine_registry=registry)
    port = _free_port()
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", ws="wsproto")
    srv = uvicorn.Server(cfg)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline and not srv.started:
        time.sleep(0.02)
    return srv, thread, f"http://127.0.0.1:{port}"


_OPEN_AND_TRACK_JS = """
async ([base, n]) => {
  const wss = [];
  const opened = [];
  for (let i = 0; i < n; i++) {
    const ws = new WebSocket(base.replace(/^http/, 'ws') + '/ws');
    wss.push(ws);
    opened.push(new Promise(r => ws.addEventListener('open', r, { once: true })));
  }
  await Promise.all(opened);
  window.__wss = wss;
  return wss.length;
}
"""

# Stagger half the closes by ~0..3ms so close frames are still landing
# at the server when the test fires force_exit. The other half stay open
# so shutdown() must call connection.shutdown() on them too. Mixed-state
# inboxes are what trip the wsproto state assertion in vanilla uvicorn.
_CLOSE_HALF_STAGGERED_JS = """
() => {
  const all = window.__wss || [];
  const half = Math.floor(all.length / 2);
  for (let i = 0; i < half; i++) {
    const ws = all[i];
    setTimeout(() => { try { ws.close(1000); } catch (e) {} }, Math.random() * 3);
  }
  return half;
}
"""


async def _spawn_ws_clients(browser, base, n):
    ctx = await browser.new_context(viewport={"width": 800, "height": 600})
    page = await ctx.new_page()
    await page.goto(base + "/")
    await page.evaluate(_OPEN_AND_TRACK_JS, [base, n])
    return ctx, page


async def _close_half_staggered(page):
    await page.evaluate(_CLOSE_HALF_STAGGERED_JS)


def _maybe_bypass_guard():
    """Restore vanilla uvicorn.WSProtocol.shutdown so the harness can
    repro against the unfixed code path. Also marks the app's guard as
    already-installed so the per-server lifespan won't re-wrap us.
    Returns a tuple of (saved_shutdown, saved_flag) so we can restore."""
    if not BYPASS_GUARD:
        return None
    import wsproto
    from uvicorn.protocols.websockets import wsproto_impl
    from sturddle_view import app as app_mod

    saved_shutdown = wsproto_impl.WSProtocol.shutdown
    saved_flag = app_mod._ws_shutdown_patched

    def _vanilla_shutdown(self):
        self.stop_keepalive()
        if self.handshake_complete:
            self.queue.put_nowait({"type": "websocket.disconnect", "code": 1012})
            output = self.conn.send(wsproto.events.CloseConnection(code=1012))
            self.transport.write(output)
        else:
            self.send_500_response()
        self.transport.close()

    wsproto_impl.WSProtocol.shutdown = _vanilla_shutdown
    # Lie to the lifespan install: tell it the guard is already in place
    # so it leaves our vanilla version alone.
    app_mod._ws_shutdown_patched = True
    return (saved_shutdown, saved_flag)


def _restore_guard(saved):
    if saved is None:
        return
    from uvicorn.protocols.websockets import wsproto_impl
    from sturddle_view import app as app_mod

    saved_shutdown, saved_flag = saved
    wsproto_impl.WSProtocol.shutdown = saved_shutdown
    app_mod._ws_shutdown_patched = saved_flag


@pytest.mark.asyncio
async def test_ws_shutdown_no_unhandled_thread_warning(tmp_path_factory, browser):
    if browser is None:
        pytest.skip("chromium not installed")

    # pytest's threadexception plugin defers warning emission to teardown,
    # so we install our own threading.excepthook to capture raw thread
    # exceptions inline. We chain to the previous hook.
    captured: list[str] = []
    prev_hook = threading.excepthook

    def _hook(args):
        tb = "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback))
        if "wsproto" in tb or "LocalProtocolError" in tb:
            captured.append(tb)
        prev_hook(args)

    threading.excepthook = _hook
    saved_shutdown = _maybe_bypass_guard()
    try:
        for i in range(ITERATIONS):
            tmp = tmp_path_factory.mktemp(f"sv_ws_stress_{i}")
            srv, thread, base = _start_server(tmp)
            ctx = None
            try:
                ctx, page = await _spawn_ws_clients(browser, base, CLIENTS_PER_ITER)
                # Half the WSs close on a random sub-3ms timer; the other
                # half stay open. Fire force_exit immediately so shutdown
                # iterates a connections set in mid-transition.
                await _close_half_staggered(page)
                srv.should_exit = True
                srv.force_exit = True
                # Match the original test_e2e_slot_grid fixture timeout: we
                # are reproducing the same shutdown race, not a slower
                # version of it.
                thread.join(timeout=2)
            finally:
                if thread.is_alive():
                    srv.force_exit = True
                    thread.join(timeout=2)
                if ctx is not None:
                    try:
                        await ctx.close()
                    except Exception:
                        pass

            print(f"iter {i + 1}/{ITERATIONS}: captured so far = {len(captured)}")
            if captured and len(captured) == 1:
                print(captured[0])
    finally:
        threading.excepthook = prev_hook
        _restore_guard(saved_shutdown)

    assert not captured, (
        f"reproduced wsproto LocalProtocolError in shutdown: "
        f"{len(captured)}/{ITERATIONS} iterations; first traceback:\n{captured[0]}"
    )
