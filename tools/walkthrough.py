"""Browser walkthrough of a running compass: log in with TOTP, visit every page, screenshot.

    uv run python tools/walkthrough.py --base http://127.0.0.1:8000 --user me \
        --password '...' --totp-secret BASE32 --shots shots/

Exits 1 when a page fails to load, logs a browser console error, or a chart does not draw.
"""
import argparse
import asyncio
import sys
from pathlib import Path

import pyotp
from playwright.async_api import async_playwright

PAGES = [
    ("/", "Dashboard", ["Conditions", "Valuation", "Trend"]),
    ("/portfolio", "Portfolio", ["Holdings", "Look-through sectors"]),
    ("/macro", "Macro and cycle dashboard", ["Shiller CAPE"]),
    ("/macro/cape", "Shiller CAPE", ["Full history"]),
    ("/rules", "Rules", ["History replay"]),
    ("/rules/ndx-ladder/history", "Replay", ["Every time it fired"]),
    ("/rules/cape-freeze/history", "Replay", []),
    ("/rules/new", "New rule", ["Rule types"]),
    ("/coach", "Coach", ["Lessons"]),
    ("/coach/lessons/valuation-cape", "Shiller CAPE", []),
    ("/journal", "Decision journal", ["New entry"]),
    ("/status", "Status", ["Data series"]),
]


async def run(args) -> int:
    shots = Path(args.shots)
    shots.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for width, label in ((1280, "desktop"), (390, "phone")):
            ctx = await browser.new_context(viewport={"width": width, "height": 900})
            page = await ctx.new_page()
            errors: list[str] = []
            page.on("console", lambda m, errs=errors: errs.append(m.text) if m.type == "error" else None)
            page.on("pageerror", lambda e, errs=errors: errs.append(str(e)))
            await page.goto(f"{args.base}/login")
            await page.fill("#username", args.user)
            await page.fill("#password", args.password)
            await page.fill("#code", pyotp.TOTP(args.totp_secret).now())
            await page.click("button[type=submit]")
            await page.wait_for_url(f"{args.base}/")
            for path, heading, texts in PAGES:
                errors.clear()
                resp = await page.goto(f"{args.base}{path}")
                await page.wait_for_timeout(600)
                body = await page.inner_text("main")
                name = path.strip("/").replace("/", "_") or "dashboard"
                await page.screenshot(path=str(shots / f"{label}-{name}.png"), full_page=True)
                if resp is None or resp.status != 200:
                    failures.append(f"{label} {path}: HTTP {resp.status if resp else '?'}")
                for t in [heading, *texts]:
                    if t not in body:
                        failures.append(f"{label} {path}: missing text {t!r}")
                charts = await page.locator("[data-chart]").count()
                drawn = await page.locator("[data-chart] canvas").count()
                empty = await page.locator("[data-chart]:has-text('No data yet')").count()
                if charts and drawn + empty < charts:
                    failures.append(f"{label} {path}: {charts - drawn - empty} charts not drawn")
                if errors:
                    failures.append(f"{label} {path}: console errors {errors}")
                if width > 500:
                    overflow = await page.evaluate(
                        "document.documentElement.scrollWidth > window.innerWidth + 1")
                    if overflow:
                        failures.append(f"{label} {path}: horizontal overflow")
            if width > 500:
                await page.goto(f"{args.base}/macro")
                await page.click("#ind-cape button:has-text('Explain this')")
                await page.wait_for_selector("#explain-cape > div", timeout=120_000)
                await page.screenshot(path=str(shots / f"{label}-explain.png"), full_page=False)
                await page.goto(f"{args.base}/journal")
                await page.fill("#title", "Walkthrough entry")
                await page.fill("#symbols", "EQQQ")
                await page.fill("#thesis", "Testing the journal from the walkthrough.")
                await page.click("form[action='/journal'] button")
                await page.wait_for_url("**/journal/*")
                if "Walkthrough entry" not in await page.inner_text("main"):
                    failures.append("journal entry not shown after save")
            await ctx.close()
        await browser.close()
    for f in failures:
        print("FAIL", f)
    print(f"{len(PAGES) * 2} page loads, {len(failures)} failures, screenshots in {shots}")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--totp-secret", required=True)
    ap.add_argument("--shots", default="shots")
    return asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
