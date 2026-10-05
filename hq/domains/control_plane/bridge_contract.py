"""The controller bridge's contract, read where a declaration states what the controller also checks.

``controller/api/hq-controller.openapi.json`` is written once. The controller
embeds it (``controller/api/contract.go``) and generates its client from it; a
declaration here takes a pattern, a default or a fixed value from it instead
of restating it, and the bridge application takes its routes, parameters and
size limit from it, so the two cannot differ.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
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


@dataclass(frozen=True)
class Parameter:
    """One query parameter of a bridge action, as the contract states it."""

    name: str
    required: bool
    schema: dict[str, Any]

    @property
    def repeated(self) -> bool:
        return self.schema.get("type") == "array"


@dataclass(frozen=True)
class Operation:
    """One bridge action: its path, its query parameters, and whether it takes a body."""

    name: str
    parameters: tuple[Parameter, ...]
    takes_body: bool


def _resolved(schema: dict[str, Any]) -> dict[str, Any]:
    """A parameter's schema, following a reference to a component schema."""

    reference = schema.get("$ref")
    if reference is None:
        return schema
    prefix = "#/components/schemas/"
    if not reference.startswith(prefix):
        raise ValueError(f"the bridge contract references {reference}, which is not a component schema")
    resolved: dict[str, Any] = contract()["components"]["schemas"][reference.removeprefix(prefix)]
    return resolved


@cache
def operations() -> dict[str, Operation]:
    """Every bridge action the contract declares, by name."""

    found: dict[str, Operation] = {}
    for path, item in contract()["paths"].items():
        if set(item) != {"post"}:
            raise ValueError(f"the bridge contract's {path} is not one POST operation")
        operation = item["post"]
        parameters = tuple(
            Parameter(entry["name"], bool(entry.get("required")), _resolved(entry["schema"]))
            for entry in operation.get("parameters", ())
        )
        if any(entry["in"] != "query" for entry in operation.get("parameters", ())):
            raise ValueError(f"the bridge contract's {path} takes a parameter outside the query")
        name = path.removeprefix("/")
        found[name] = Operation(name, parameters, "requestBody" in operation)
    return found


def max_body_bytes() -> int:
    """The most bytes one bridge request or answer may be."""

    return limit("BridgeBody", "maxLength")
