"""The Design page's gallery: every entry of the component catalog in one document.

The catalog is test code, so it reaches HQ through this module alone (an
import contract in pyproject.toml holds that). The gallery is the document the
catalog's survey judges (`catalog.page`), framed by the Design page, so its
example ids, headings and links stay inside it and never meet a page's rules.
The image is the file its build wrote with `manage.py design_gallery` to
SEVERINO_DESIGN_GALLERY, after the assets were collected, so its asset
addresses are the image's own, at an address named for its bytes. A checkout
names no file, whatever DEBUG says, and its server draws the catalog as it
stands on each request for the gallery, at the address ``LIVE``; the page that
frames it draws nothing of the catalog's.
"""

import hashlib
from functools import cache
from pathlib import Path

from django.conf import settings

lazy from hq.platform.core.tests.browser import catalog

# The name of the gallery a server draws on each request, which no digest is.
LIVE = "live"


def drawn() -> bytes:
    """The gallery document as the catalog draws it."""

    return catalog.page(catalog.GALLERY).encode()


@cache
def _built(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _named_file() -> Path | None:
    path = settings.SEVERINO_DESIGN_GALLERY
    return None if path is None else Path(path)


def located() -> str | None:
    """The name the Design page frames the gallery at, or None where the
    build named a file and wrote none there."""

    if (path := _named_file()) is None:
        return LIVE
    body = _built(path)
    return named(body) if body else None


def document(name: str) -> bytes | None:
    """The gallery at ``name``, or None where none answers to it."""

    if (path := _named_file()) is None:
        return drawn() if name == LIVE else None
    body = _built(path)
    return body if body and named(body) == name else None


def named(body: bytes) -> str:
    """The name of one gallery's bytes, so its address changes with them and
    a browser keeps each one as long as it likes."""

    return hashlib.sha256(body).hexdigest()[:16]


def write(path: Path) -> int:
    """Draw the gallery into ``path``, returning its size in bytes."""

    body = drawn()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return len(body)
