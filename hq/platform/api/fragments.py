"""What the API document's builder and its checks share: its build failure and its component names.

That the document says each described fragment once is held by
``tests/test_described_once.py``, a gate, never a step of serving it.
"""

import re

from django.utils.text import capfirst


class OpenAPIError(RuntimeError):
    """A route the document cannot describe; fails the build, not the client."""


def pascal(name: str) -> str:
    """``name``'s words run together, each capitalized: ``asset.create`` is ``AssetCreate``."""

    return "".join(capfirst(part) for part in re.split(r"[^0-9A-Za-z]+", name) if part)
