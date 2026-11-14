"""The Design page's gallery: every entry of the component catalog in one document.

The catalog is test code, so it reaches HQ through this module alone (an
import contract in pyproject.toml holds that). The gallery is the document the
catalog's survey judges (`catalog.page`), framed by the Design page, so its
example ids, headings and links stay inside it and never meet a page's rules.
A dev server draws it live, so a change to a part shows on the next load;
anywhere else it is the file the image build wrote with `manage.py
design_gallery`, after the assets were collected, so its asset addresses are
the image's own.
"""

import hashlib
from functools import cache
from pathlib import Path

from django.conf import settings

lazy from hq.platform.core.tests.browser import catalog


def drawn() -> bytes:
    """The gallery document as the catalog draws it."""

    return catalog.page(catalog.GALLERY).encode()


@cache
def _built(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def document() -> bytes | None:
    """The gallery document, or None where no build wrote it."""

    return drawn() if settings.DEBUG else _built(Path(settings.SEVERINO_DESIGN_GALLERY))


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
