"""The docs' screenshots, each drawn from its declaration in `SHOTS` on the
built showcase demo in WebKit, so they show the example estate, never a real
HQ, and are redrawn whole after a page changes.
"""

import sys
from dataclasses import dataclass
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

from demo.serve import serving

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "var" / "demo"
OUT = ROOT / "docs" / "images"
# Twice the CSS pixels, so a shot stays sharp on a high-density screen when
# GitHub scales it down to its column.
SCALE = 2
# Space kept around a framed subject, in CSS pixels.
MARGIN = 20


@dataclass(frozen=True, slots=True)
class Shot:
    """One picture: where it is written under docs/images, the demo page it
    shows, and what of it.

    ``follow`` names a link on ``page`` whose page is shown instead, for a page
    whose address the build numbers. ``frame`` names Playwright locators: the
    shot runs the window's width from the top of the first thing they match to
    the bottom of the last, with `MARGIN` above it and ``below`` under it;
    without one it is the window as it opens. A frame that ends inside a list
    ends at its last row's edge (``below=0``), so the next row is not shown
    cut through.
    """

    path: str
    page: str
    width: int = 1360
    height: int = 900
    follow: str = ""
    frame: tuple[str, ...] = ()
    below: int = MARGIN


SHOTS = (
    Shot("dashboard.png", "/", width=1440),
    Shot("service.png", "/infrastructure/services/hq.example.com/", width=1440),
    Shot("ai/agents.png", "/agents/", frame=("header.site-header", "summary:has-text('Every other action')")),
    Shot("ai/audit.png", "/audit/", frame=("main .page-head", "main tr:has-text('Access denied')"), below=0),
    Shot(
        "ai/approvals.png",
        "/audit/?awaiting=1",
        follow="main tbody a",
        frame=("main .page-head", "main .approval-card"),
    ),
)


def _open(page: Page, origin: str, shot: Shot) -> None:
    page.set_viewport_size({"width": shot.width, "height": shot.height})
    page.goto(f"{origin}{shot.page}", wait_until="networkidle")
    if shot.follow:
        link = page.locator(shot.follow).first
        page.goto(f"{origin}{link.get_attribute('href')}", wait_until="networkidle")
    # The docs picture HQ itself, so the demo's own corner chip stays out of them.
    page.add_style_tag(content=".demo-foot { display: none; }")


def _clip(page: Page, shot: Shot) -> dict[str, float] | None:
    """The band of the page ``shot.frame`` spans, or nothing for the window.
    A locator that matches nothing is refused rather than shot around."""

    if not shot.frame:
        return None
    boxes = []
    for selector in shot.frame:
        found = page.locator(selector)
        if not found.count():
            raise LookupError(f"{shot.path}: nothing on {page.url} matches {selector!r}")
        boxes += [box for box in (each.bounding_box() for each in found.all()) if box]
    scrolled = page.evaluate("() => window.scrollY")
    top = max(0, min(box["y"] for box in boxes) + scrolled - MARGIN)
    bottom = max(box["y"] + box["height"] for box in boxes) + scrolled + shot.below
    return {"x": 0, "y": top, "width": shot.width, "height": bottom - top}


def main() -> int:
    if not (SITE / "index.html").exists():
        print(f"no demo at {SITE}: run mise run demo:build", file=sys.stderr)
        return 1
    with serving(SITE) as origin, sync_playwright() as playwright:
        browser = playwright.webkit.launch()
        page = browser.new_page(device_scale_factor=SCALE, color_scheme="light")
        for shot in SHOTS:
            _open(page, origin, shot)
            clip = _clip(page, shot)
            page.screenshot(
                path=str(OUT / shot.path), full_page=clip is not None, clip=clip, animations="disabled", caret="hide"
            )
            print(f"wrote docs/images/{shot.path}")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
