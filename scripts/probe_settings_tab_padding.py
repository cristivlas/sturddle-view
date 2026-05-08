"""Probe settings dialog's tab rail to figure out where the focused tab's
outline ring is being clipped. One-off — measures wa-tab-group base/nav
padding plus the first tab's geometry vs the dialog body's top edge."""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright


async def main() -> None:
    async with async_playwright() as pw:
        b = await pw.chromium.launch(headless=True)
        ctx = await b.new_context(viewport={"width": 1280, "height": 900})
        p = await ctx.new_page()
        await p.goto("http://127.0.0.1:8765/")
        await p.wait_for_timeout(1500)
        await p.evaluate(
            """
            () => {
              const all = Array.from(document.querySelectorAll("button, [aria-label]"));
              const gear = all.find(el => (el.getAttribute("aria-label") || "").toLowerCase().includes("settings"))
                        || all.find(el => (el.title || "").toLowerCase().includes("settings"));
              if (gear) gear.click();
            }
            """,
        )
        await p.wait_for_timeout(800)
        m = await p.evaluate(
            """
            () => {
              const dlg = document.querySelector("wa-dialog");
              const tabs = dlg.querySelector(".settings-tabs");
              const sr = tabs.shadowRoot;
              const out = [];
              if (sr) {
                for (const part of ["base", "nav", "tabs"]) {
                  const sel = `[part="${part}"]`;
                  const el = sr.querySelector(sel);
                  if (el) {
                    const r = el.getBoundingClientRect();
                    const cs = getComputedStyle(el);
                    out.push({part, l: r.left, t: r.top, r: r.right, b: r.bottom,
                              padL: cs.paddingLeft, padT: cs.paddingTop,
                              padR: cs.paddingRight, padB: cs.paddingBottom});
                  }
                }
              }
              const firstTab = dlg.querySelector(".settings-tabs wa-tab");
              if (firstTab) {
                const r = firstTab.getBoundingClientRect();
                out.push({tab: firstTab.textContent.trim(), l: r.left, t: r.top, r: r.right, b: r.bottom});
              }
              const body = dlg.shadowRoot.querySelector('[part="body"]');
              const bodyRect = body.getBoundingClientRect();
              const bodyCs = getComputedStyle(body);
              out.push({body: {l: bodyRect.left, t: bodyRect.top, r: bodyRect.right, b: bodyRect.bottom,
                               padT: bodyCs.paddingTop, padL: bodyCs.paddingLeft}});
              return out;
            }
            """,
        )
        for e in m:
            print(e)
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
