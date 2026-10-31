"""The controller bridge's contract: what the bridge alone defines, joined to
what the registry declares.

``bridge_base.json`` holds the bridge's own messages, written by hand, and
``bridge_registry`` builds the rest from the registry's declarations. The join
is held in memory, where the bridge application takes its routes and payload
schemas from it: docs/Architecture.md#the-controller.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings

# jsonschema ships without inline type information.
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from hq.platform.application.ui import ended

lazy from .bridge_registry import connections_document, schemas
lazy from .connection_shapes import renderer_registry

# What the bridge alone defines, written by hand.
BASE_PATH = Path(__file__).with_name("bridge_base.json")
# The joined document, written for the controller's generator.
CONTRACT_PATH = Path(settings.BASE_DIR) / "controller" / "api" / "hq-controller.openapi.json"
# The connections document's schema, written for the same generator.
CONNECTIONS_PATH = Path(settings.BASE_DIR) / "controller" / "api" / "hq-connections.openapi.json"
# The shapes a vault item is projected into, written for the secret renderer.
SHAPES_PATH = Path(settings.BASE_DIR) / "hq" / "config" / "controller-connections.json"
# The name the contract's own references resolve against.
CONTRACT_URI = "urn:hq:controller-bridge"
_REFERENCE = "#/components/schemas/"


def _references(node: Any) -> Iterator[str]:
    """Every component a document refers to."""

    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str):
            yield reference
        for value in node.values():
            yield from _references(value)
    elif isinstance(node, list):
        for value in node:
            yield from _references(value)


def joined(base: dict[str, Any], declared: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The base with the registry's schemas among its components.

    A name both state, or a reference to a component neither states, is an
    error: a fact has one owner, and a message cannot name a schema that is
    not there.
    """

    own: dict[str, Any] = base["components"]["schemas"]
    twice = sorted(set(own) & set(declared))
    if twice:
        raise ValueError(f"the bridge base restates what the registry declares: {twice}")
    schemas = {**own, **declared}
    missing = sorted(
        {
            reference
            for reference in _references({"paths": base["paths"], "schemas": schemas})
            if reference.removeprefix(_REFERENCE) not in schemas
        }
    )
    if missing:
        raise ValueError(f"the bridge contract refers to schemas nothing declares: {missing}")
    return {**base, "components": {**base["components"], "schemas": schemas}}


@cache
def contract() -> dict[str, Any]:
    with BASE_PATH.open(encoding="utf-8") as source:
        base: dict[str, Any] = json.load(source)
    return joined(base, schemas())


def render(document: dict[str, Any]) -> str:
    """A document as the file that carries it."""

    return json.dumps(document, indent=2) + "\n"


def emitted() -> dict[Path, str]:
    """Every file written from the registry, with its content."""

    return {
        CONTRACT_PATH: render(contract()),
        CONNECTIONS_PATH: render(connections_document()),
        SHAPES_PATH: render(renderer_registry()),
    }


def limit(schema: str, *path: str | int) -> int:
    """One positive integer keyword of a component schema, such as a ``maxLength``."""

    node: Any = contract()["components"]["schemas"][schema]
    for key in path:
        node = node[key]
    if isinstance(node, bool) or not isinstance(node, int) or node <= 0:
        raise ValueError(f"the bridge contract's {'.'.join(map(str, (schema, *path)))} is not a limit")
    return node


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
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
    pointer = "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in error.absolute_path)
    expected = error.validator_value
    # A best match always names the keyword it failed; an error without one
    # is still a departure, said without its reason.
    keyword = error.validator if isinstance(error.validator, str) else ""
    if keyword in ("required", "additionalProperties"):
        # Member names only, which the contract itself publishes.
        reason = f"{_REASONS[keyword]}: {_members(error)}"
    else:
        reason = _REASONS.get(keyword, "does not match the contract").format(
            expected=" or ".join(expected) if isinstance(expected, list) else expected
        )
    return ended(f"The {subject} at {pointer or '/'} {reason}")


def _members(error: ValidationError) -> str:
    """The members a ``required`` or ``additionalProperties`` failure is about."""

    held = set(error.instance) if isinstance(error.instance, dict) else set()
    if error.validator == "required":
        required = error.validator_value if isinstance(error.validator_value, list) else []
        names = [name for name in required if name not in held]
    else:
        schema = error.schema if isinstance(error.schema, Mapping) else {}
        names = sorted(held - set(schema.get("properties", ())))
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
    validator = Draft202012Validator({"$ref": f"{CONTRACT_URI}#/components/schemas/{name}"}, registry=_registry())
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
