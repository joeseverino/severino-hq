"""The controller bridge's contract, read where a declaration states what the controller also checks.

``controller/api/hq-controller.openapi.json`` is written once. The controller
embeds it (``controller/api/contract.go``); a declaration here takes a pattern,
a default or a fixed value from it instead of restating it, so the two cannot
differ.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings

CONTRACT_PATH = Path(settings.BASE_DIR) / "controller" / "api" / "hq-controller.openapi.json"


@cache
def contract() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    return document


def _node(schema: str, path: tuple[str | int, ...]) -> Any:
    node: Any = contract()["components"]["schemas"][schema]
    for key in path:
        node = node[key]
    return node


def keyword(schema: str, *path: str | int) -> str:
    """One string keyword of a component schema, by the keys and indexes under it.

    A keyword the contract does not state fails the import that asked for it.
    """

    node = _node(schema, path)
    if not isinstance(node, str) or not node:
        raise ValueError(f"the bridge contract's {'.'.join(map(str, (schema, *path)))} is not a value")
    return node


def limit(schema: str, *path: str | int) -> int:
    """One positive integer keyword, such as a ``maxLength``."""

    node = _node(schema, path)
    if isinstance(node, bool) or not isinstance(node, int) or node <= 0:
        raise ValueError(f"the bridge contract's {'.'.join(map(str, (schema, *path)))} is not a limit")
    return node
