"""The Design page's gallery: every entry of the component catalog, as the
page's content.

The catalog is test code, so it reaches HQ through this module alone (an
import contract in pyproject.toml holds that). The image draws the markup its
build wrote with `manage.py design_gallery` to SEVERINO_DESIGN_GALLERY, after
the assets were collected, so its asset addresses are the image's own. A
checkout names no file, whatever DEBUG says, and its server draws the catalog
as it stands on each request for the page.
"""

from functools import cache
from pathlib import Path

from django.conf import settings
from django.utils.safestring import SafeString, mark_safe

lazy from hq.platform.core.tests.browser import catalog


def drawn() -> str:
    """The gallery as the catalog draws it."""

    return catalog.gallery_body()


@cache
def _built(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def markup() -> SafeString | None:
    """The gallery the Design page draws, or None where the build named a
    file and wrote none there."""

    path = settings.SEVERINO_DESIGN_GALLERY
    body = drawn() if path is None else _built(Path(path))
    return mark_safe(body) if body else None  # noqa: S308 - the catalog's own markup, drawn by HQ's templates


def write(path: Path) -> int:
    """Draw the gallery into ``path``, returning its size in bytes."""

    body = drawn().encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return len(body)
