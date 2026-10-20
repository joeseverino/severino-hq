"""The README's pictures, taken from the built showcase demo in WebKit.

They show the example estate, never a real HQ.
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

from demo.serve import serving

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "var" / "demo"
OUT = ROOT / "docs" / "images"

# Each picture's file name and the demo page it shows.
SHOTS = {
    "dashboard": "/",
    "service": "/infrastructure/services/hq.example.com/",
}


def main() -> int:
    if not (SITE / "index.html").exists():
        print(f"no demo at {SITE}: run mise run demo:build", file=sys.stderr)
        return 1
    with serving(SITE) as origin, sync_playwright() as playwright:
        browser = playwright.webkit.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900}, device_scale_factor=2, color_scheme="light")
        for name, path in SHOTS.items():
            page.goto(f"{origin}{path}", wait_until="networkidle")
            page.screenshot(path=str(OUT / f"{name}.png"))
            print(f"wrote docs/images/{name}.png")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
