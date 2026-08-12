"""Screenshot the dashboard at desktop and phone widths (dev tool)."""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

URL = os.environ.get("PREVIEW_URL", "http://127.0.0.1:8777/")
OUT = sys.argv[1] if len(sys.argv) > 1 else "/tmp/shots"
PROXY = os.environ.get("HTTPS_PROXY")

os.makedirs(OUT, exist_ok=True)

SIZES = [
    ("desktop", 1440, 900, False),
    ("laptop", 1180, 800, False),
    ("phone", 390, 844, True),
]

with sync_playwright() as p:
    launch = {"args": ["--force-color-profile=srgb", "--font-render-hinting=none"]}
    if PROXY and os.environ.get("USE_PROXY"):
        launch["proxy"] = {"server": PROXY, "bypass": "<-loopback>"}
    browser = p.chromium.launch(**launch)
    for name, w, h, mobile in SIZES:
        ctx = browser.new_context(
            viewport={"width": w, "height": h},
            device_scale_factor=2,
            is_mobile=mobile,
            has_touch=mobile,
        )
        page = ctx.new_page()
        # Not `networkidle` — the SSE stream never goes idle by design.
        page.goto(URL, wait_until="domcontentloaded")
        page.wait_for_selector(".wl-row", timeout=15000)
        page.wait_for_timeout(2600)  # let the load animations settle
        page.screenshot(path=f"{OUT}/{name}.png")
        if mobile:
            for tab in ("tape", "setup"):
                page.click(f'nav.tabs button[data-tab="{tab}"]')
                page.wait_for_timeout(900)
                page.screenshot(path=f"{OUT}/{name}-{tab}.png")
        else:
            page.click('.wl-row[data-sym="RELIANCE.NS"]')
            page.wait_for_timeout(1900)
            page.screenshot(path=f"{OUT}/{name}-selected.png")
            page.fill("#q", "tata")
            page.wait_for_timeout(900)
            page.screenshot(path=f"{OUT}/{name}-search.png")
        ctx.close()
    browser.close()
print("wrote screenshots to", OUT)
