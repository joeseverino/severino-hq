"""The example's transport: one question to a registry outside the process.

Nothing here touches the database, and nothing calls it from a request: it is
reached only from the outbound work in ``outbound``.
"""

import json
from urllib.request import urlopen

REGISTRY = "https://registry.example/notes"
TIMEOUT_SECONDS = 10


def read(slug: str) -> list[str] | None:
    """What the registry lists for one note, or None when it lists nothing."""

    with urlopen(f"{REGISTRY}/{slug}", timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
        listed = json.load(response)
    return listed or None
