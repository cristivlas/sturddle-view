"""One-off DOM probe: figure out which element is actually scrolling
when the engine settings dialog's Launch tab overflows.

Drives the running dev server at http://127.0.0.1:8765 (no-auth) via
Playwright. Opens the Engines roster, picks the first engine, opens its
settings, switches to Launch, repeatedly clicks the env "+" button until
the panel overflows, then walks the ancestor chain from the visible
trash icon up to the dialog and reports which elements have a scrollbar
(scrollHeight > clientHeight) and where the scrollTop ended up.
"""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

URL = "http://127.0.0.1:8765/"


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(URL)

        # Switch to Engines perspective.
        await page.wait_for_timeout(1500)
        await page.evaluate("""
          () => {
            const btns = Array.from(document.querySelectorAll('button, [role=\"tab\"], a'));
            const engines = btns.find(b => b.textContent && b.textContent.trim() === 'Engines');
            if (engines) engines.click();
          }
        """)
        await page.wait_for_timeout(500)

        # Pick the rc9 engine — it has lots of UCI options.
        await page.wait_for_timeout(500)
        await page.screenshot(path="scripts/_eng_perspective.png", full_page=True)
        rows_text = await page.evaluate("() => Array.from(document.querySelectorAll('.engines-list-item')).map(r => r.textContent.trim().slice(0, 120))")
        print("rows:", rows_text)
        if not rows_text:
            raise SystemExit("no engines rows visible")
        await page.locator(".engines-list-item", has_text="rc9.050226").first.click()
        await page.wait_for_timeout(200)
        await page.locator(".engines-detail-options").click(force=True)
        await page.wait_for_timeout(800)
        dlg_count = await page.evaluate("() => document.querySelectorAll('wa-dialog').length")
        print("dialogs after click:", dlg_count)
        if dlg_count == 0:
            await page.screenshot(path="scripts/_after_click.png", full_page=True)
            raise SystemExit("dialog did not open for rc9")
        print("opened dialog on rc9 engine")

        await page.screenshot(path="scripts/_probe_state1.png", full_page=True)

        await page.wait_for_timeout(500)
        # Add a Launch arg --dev-mode so the next probe re-spawns the
        # engine with extra options exposed; switch to Launch tab first.
        await page.evaluate("""
          () => {
            const dlg = document.querySelector('wa-dialog');
            if (!dlg) return;
            const tab = dlg.querySelector('wa-tab[panel="launch"]');
            if (tab) tab.click();
          }
        """)
        await page.wait_for_timeout(300)
        # Add an argument row and type --dev-mode into it.
        await page.locator(".engine-launch-section-add").first.click()
        await page.wait_for_timeout(150)
        # wa-input is a custom element; set its value via its property
        # and dispatch an input event to trigger our handler.
        await page.evaluate("""
          () => {
            const dlg = document.querySelector('wa-dialog');
            const inp = dlg.querySelector('.engine-launch-args-row wa-input');
            inp.value = '--dev-mode';
            inp.dispatchEvent(new Event('input', { bubbles: true }));
          }
        """)
        await page.wait_for_timeout(150)
        # Refresh probes with the in-progress profile and reopens with
        # the new schema.
        await page.evaluate("""
          () => {
            const dlg = document.querySelector('wa-dialog');
            if (!dlg) return;
            const refresh = Array.from(dlg.querySelectorAll('wa-button')).find(b => b.textContent.trim() === 'Refresh');
            if (refresh) refresh.click();
          }
        """)
        # The dialog re-opens. Wait for the Launch panel to be replaced
        # by Options panel content (we re-open on Options by default).
        await page.wait_for_timeout(1500)

        # Now scroll inside the Options form via wheel to confirm scroll
        # goes through the form, not the rail.
        form = page.locator(".engine-opt-form")
        await form.wait_for(state="visible")
        box = await form.bounding_box()
        if box:
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            await page.mouse.wheel(0, 800)
            await page.wait_for_timeout(200)

        # Walk ancestors from the form up to the dialog and report which
        # elements have a scrollbar and their scrollTop. Use evaluate so
        # we can step through shadow DOM if needed.
        report = await page.evaluate(
            r"""
            () => {
              const out = [];
              const dialog = document.querySelector("wa-dialog");
              if (!dialog) return { error: "no open dialog" };
              const form = dialog.querySelector(".engine-opt-form")
                          || dialog.querySelector(".engine-launch-form");
              if (!form) return { error: "no form" };
              // Walk up the composed tree: parentElement first, then
              // shadow host on shadow boundary.
              let node = form;
              const seen = new Set();
              while (node && node !== document.documentElement && !seen.has(node)) {
                seen.add(node);
                const tag = node.tagName?.toLowerCase() || "?";
                const cls = node.className?.toString?.() || "";
                const cs = getComputedStyle(node);
                out.push({
                  tag,
                  cls,
                  scrollTop: node.scrollTop,
                  scrollHeight: node.scrollHeight,
                  clientHeight: node.clientHeight,
                  overflowY: cs.overflowY,
                  position: cs.position,
                  height: cs.height,
                });
                // Move up: parent in light DOM, then host across shadow.
                if (node.parentElement) {
                  node = node.parentElement;
                } else if (node.parentNode && node.parentNode.host) {
                  node = node.parentNode.host;
                } else {
                  break;
                }
              }
              return { chain: out };
            }
            """,
        )
        print("=== ancestor chain from form upward ===")
        if "error" in report:
            print("ERROR:", report["error"])
        else:
            for e in report["chain"]:
                scrolls = e["scrollHeight"] > e["clientHeight"]
                tag = e["tag"]
                marker = "  ** SCROLLS **" if scrolls and e["scrollTop"] != 0 else (
                    "  (overflows)" if scrolls else ""
                )
                cls_short = " ".join(e["cls"].split()[:3])
                print(
                    f"<{tag}> cls={cls_short!r}\n"
                    f"  scrollTop={e['scrollTop']} scrollH={e['scrollHeight']} clientH={e['clientHeight']} "
                    f"overflowY={e['overflowY']} pos={e['position']} height={e['height']}"
                    f"{marker}"
                )

        # Also probe the wa-tab-group's shadow root for any scrolling
        # inner container.
        shadow_report = await page.evaluate(
            r"""
            () => {
              const tg = document.querySelector("wa-dialog wa-tab-group");
              if (!tg || !tg.shadowRoot) return null;
              const out = [];
              const walk = (root, path) => {
                for (const el of root.querySelectorAll("*")) {
                  const cs = getComputedStyle(el);
                  const overflows = el.scrollHeight > el.clientHeight;
                  if (overflows || cs.overflowY === "auto" || cs.overflowY === "scroll") {
                    out.push({
                      path: path + "/" + el.tagName.toLowerCase() + (el.className ? "." + el.className : ""),
                      scrollTop: el.scrollTop,
                      scrollHeight: el.scrollHeight,
                      clientHeight: el.clientHeight,
                      overflowY: cs.overflowY,
                    });
                  }
                  if (el.shadowRoot) walk(el.shadowRoot, path + "/" + el.tagName.toLowerCase() + "::shadow");
                }
              };
              walk(tg.shadowRoot, "wa-tab-group::shadow");
              return out;
            }
            """,
        )
        print("\n=== wa-tab-group shadow scrollers ===")
        if shadow_report is None:
            print("(no shadow root)")
        else:
            for e in shadow_report:
                print(f"  {e['path']}  scrollTop={e['scrollTop']} scrollH={e['scrollHeight']} clientH={e['clientHeight']} overflowY={e['overflowY']}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
