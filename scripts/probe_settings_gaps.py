"""Measure Settings dialog: left gap (dialog edge to tab text) vs
right gap (rightmost input edge to dialog edge). Goal: trim dialog
width until they're roughly equal."""
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
        await p.evaluate("""
          () => {
            const all = Array.from(document.querySelectorAll("button, [aria-label]"));
            const gear = all.find(el => (el.getAttribute("aria-label") || "").toLowerCase().includes("settings"))
                      || all.find(el => (el.title || "").toLowerCase().includes("settings"));
            if (gear) gear.click();
          }
        """)
        await p.wait_for_timeout(800)
        for tabName in ["general", "play", "tournament"]:
            await p.evaluate(f"""
              () => {{
                const dlg = document.querySelector("wa-dialog");
                const tab = Array.from(dlg.querySelectorAll("wa-tab")).find(t => (t.getAttribute("panel") || t.panel) === "{tabName}");
                if (tab) tab.click();
              }}
            """)
            await p.wait_for_timeout(400)
            m = await p.evaluate("""
              () => {
                const dlg = document.querySelector("wa-dialog");
                const dlgRect = dlg.shadowRoot.querySelector('[part="dialog"]').getBoundingClientRect();
                const tab = dlg.querySelector("wa-tab");
                const tabBaseRect = tab.shadowRoot.querySelector('[part="base"]').getBoundingClientRect();
                const panel = Array.from(dlg.querySelectorAll("wa-tab-panel")).find(p => getComputedStyle(p).display !== "none");
                let maxRight = 0;
                for (const c of panel.children) {
                  const r = c.getBoundingClientRect();
                  if (r.right > maxRight) maxRight = r.right;
                }
                return {
                  dlgL: dlgRect.left, dlgR: dlgRect.right, dlgW: dlgRect.width,
                  tabTextL: tabBaseRect.left,
                  inputR: maxRight,
                  leftGap: tabBaseRect.left - dlgRect.left,
                  rightGap: dlgRect.right - maxRight,
                };
              }
            """)
            print(tabName, m)
        await b.close()


if __name__ == "__main__":
    asyncio.run(main())
