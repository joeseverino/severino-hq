"""The controller bridge's contract, read where a declaration states what the controller also checks.

``controller/api/hq-controller.openapi.json`` is written once. The controller
embeds it (``controller/api/contract.go``) and generates its client from it; a
declaration here takes a pattern, a default or a fixed value from it instead
of restating it, and the bridge application takes its routes, parameters,
size limit and the shape of every payload from it, so the two cannot differ.

A payload is held to the schema its operation declares, which is JSON Schema
2020-12 as OpenAPI 3.2 embeds it. What an action is handed has the types, the
required members and the bounds the contract states, so an action reads a
member and never coerces one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
# jsonschema ships without inline type information.
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from jsonschema.exceptions import ValidationError, best_match  # type: ignore[import-untyped]
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

CONTRACT_PATH = Path(settings.BASE_DIR) / "controller" / "api" / "hq-controller.openapi.json"
# The name the contract's own references resolve against.
CONTRACT_URI = "urn:hq:controller-bridge"


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


# What a violation says, by the keyword that failed. The rejected value is
# never repeated: a report carries what a provider holds.
_REASONS = {
    "type": "is not of type {expected}",
    "enum": "is not one of the values the contract allows",
    "minimum": "is less than {expected}",
    "maximum": "is more than {expected}",
    "maxLength": "is longer than {expected}",
    "required": "lacks a required member",
    "additionalProperties": "has a member the contract does not declare",
}


@dataclass(frozen=True)
class Operation:
    """One bridge action: its path, its query parameters, and the payload it takes."""

    name: str
    parameters: tuple[Parameter, ...]
    # The validator of the request body's schema; None when the action takes none.
    body: Any = None

    @property
    def takes_body(self) -> bool:
        return self.body is not None

    def violation(self, payload: Any) -> str | None:
        """Where ``payload`` first departs from the contract, or None when it conforms.

        Named by its JSON Pointer into the payload, so the sender can find the
        member; the whole payload is refused, as the controller refuses an
        answer it cannot decode.
        """

        if self.body is None:
            return None
        return _violation(self.body, f"{self.name} payload", payload)


def _violation(validator: Any, subject: str, value: Any) -> str | None:
    """Where ``value`` first departs from the validator's schema, in words."""

    error: ValidationError | None = best_match(validator.iter_errors(value))
    if error is None:
        return None
    pointer = "".join(
        "/" + str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path
    )
    expected = error.validator_value
    if error.validator in ("required", "additionalProperties"):
        # Member names only, which the contract itself publishes.
        reason = f"{_REASONS[error.validator]}: {_members(error)}"
    else:
        reason = _REASONS.get(error.validator, "does not match the contract").format(
            expected=" or ".join(expected) if isinstance(expected, list) else expected
        )
    return f"The {subject} at {pointer or '/'} {reason}."


def _members(error: ValidationError) -> str:
    """The members a ``required`` or ``additionalProperties`` failure is about."""

    held = set(error.instance) if isinstance(error.instance, dict) else set()
    if error.validator == "required":
        names = [name for name in error.validator_value if name not in held]
    else:
        names = sorted(held - set(error.schema.get("properties", ())))
    return ", ".join(str(name)[:80] for name in names[:5])


@cache
def _registry() -> Registry:
    """The contract as the resource its own references resolve in."""

    resource = Resource.from_contents(contract(), default_specification=DRAFT202012)
    registry: Registry = Registry().with_resource(CONTRACT_URI, resource)
    return registry


def departs(name: str, value: Any) -> str | None:
    """Where ``value`` departs from the component schema ``name``, or None.

    For a value built outside a request, such as a report a test composes.
    """

    if name not in contract()["components"]["schemas"]:
        raise ValueError(f"the bridge contract has no {name} schema")
    validator = Draft202012Validator(
        {"$ref": f"{CONTRACT_URI}#/components/schemas/{name}"}, registry=_registry()
    )
    return _violation(validator, name, value)


def _body_validator(path: str, operation: dict[str, Any]) -> Any:
    """The validator of one operation's JSON request body; None when it has none."""

    if "requestBody" not in operation:
        return None
    body = operation["requestBody"]
    if not body.get("required") or set(body["content"]) != {"application/json"}:
        raise ValueError(f"the bridge contract's {path} payload is not one required JSON body")
    schema = body["content"]["application/json"]["schema"]
    Draft202012Validator.check_schema(schema)
    escaped = path.replace("~", "~0").replace("/", "~1")
    pointer = f"{CONTRACT_URI}#/paths/{escaped}/post/requestBody/content/application~1json/schema"
    return Draft202012Validator({"$ref": pointer}, registry=_registry())


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
        found[name] = Operation(name, parameters, _body_validator(path, operation))
    return found


def max_body_bytes() -> int:
    """The most bytes one bridge request or answer may be."""

    return limit("BridgeBody", "maxLength")
