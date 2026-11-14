"""A screenshot of a page, which every browser tool takes through ``shoot_page``.

A browser refuses an image past 32767 px on a side, and takes minutes to shoot
and cut a page anywhere near it (the API reference with a tag opened runs far
past it), so a whole-page shot stops at ``TALLEST`` and says what it left out.
A band is one window tall wherever it starts, so the bands cut beside a shot
reach the page's end, and the rule the shot illustrates reads the whole page.
"""

from os import PathLike
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.sync_api import Page

TALLEST = 30_000

_EXTENT = "() => [document.documentElement.scrollWidth, document.documentElement.scrollHeight]"


def extent_of(page: Page) -> tuple[int, int]:
    """``page``'s width and height as a full-page shot takes them."""

    wide, tall = page.evaluate(_EXTENT)
    return wide, tall


def shoot_page(
    page: Page,
    path: str | PathLike[str],
    *,
    extent: tuple[int, int] | None = None,
    band: tuple[int, int] | None = None,
    **options: Any,
) -> str:
    """``page`` saved at ``path``: the ``band`` (top, height) given, wherever
    it starts, or the whole page, ending at ``TALLEST`` at most. ``extent`` is the page's
    (width, height) as the caller read it, or read here. Returns what the
    shot left out, as words a caption takes, or "" when it holds all it was
    asked for."""

    wide, tall = extent or extent_of(page)
    top, height = band or (0, tall)
    bottom = top + height if band else min(height, TALLEST)
    clip = {"x": 0, "y": top, "width": wide, "height": max(bottom - top, 1)}
    page.screenshot(path=path, full_page=True, clip=clip, **options)
    return f"its first {bottom} px of {tall}" if bottom < top + height else ""
