"""Every command the composed catalog runs is closed at every depth.

A field a command does not have, sent at any level of its payload, is refused
as ``invalid_input`` naming that field's path, and the schema HQ publishes for
the command says so at the same level. Walked from each capability's published
schema, so a nested type added to any command, the host's or an extension's,
is held without a case of its own. A type whose own config declares
``extra="allow"`` is open on purpose, and is the one level left open.
"""

from collections.abc import Iterator, Mapping
from typing import Any

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from pydantic import TypeAdapter, ValidationError

from hq.domains.control_plane.models import CapabilityRule

from ..capabilities import capability_registry, execute_capability, validate_command
from ..capability_policy import SET_POLICY
from ..input_errors import pydantic_refusal
from ..integration_specs import capability_schema
from ..json_schemas import LOCAL, json_schema
from ..security import web_principal
from .writers import keyed

# A field no command has.
_UNKNOWN = "example_unknown_field"


type _Reached = Iterator[tuple[tuple[Any, ...], Any, Mapping[str, Any]]]


def _objects(schema: Mapping[str, Any], definitions: Mapping[str, Any], seen: frozenset[str] = frozenset()) -> _Reached:
    """``(path, payload, node)`` for each object with fields ``schema`` reaches:
    a payload that carries the unknown field at that object, and the object's
    schema."""

    if (ref := schema.get("$ref", "")).startswith(LOCAL):
        name = ref.removeprefix(LOCAL)
        if name not in seen:
            yield from _objects(definitions[name], definitions, seen | {name})
    for branch in (*schema.get("anyOf", ()), *schema.get("oneOf", ()), *schema.get("allOf", ())):
        yield from _objects(branch, definitions, seen)
    for step, wrap, inner in _members(schema):
        for path, payload, node in _objects(inner, definitions, seen):
            yield (step, *path), wrap(payload), node
    if properties := schema.get("properties"):
        # A tag says which branch of a choice the object is.
        tags = {name: field["const"] for name, field in properties.items() if "const" in field}
        yield (), {**tags, _UNKNOWN: True}, schema
        for name, field in properties.items():
            for path, payload, node in _objects(field, definitions, seen):
                yield (name, *path), {**tags, name: payload}, node


def _members(schema: Mapping[str, Any]) -> Iterator[tuple[Any, Any, Mapping[str, Any]]]:
    """``(step, wrap, schema)`` for each member of a list, tuple or map: the
    step its path takes, and how a payload for the member is held there."""

    if isinstance(items := schema.get("items"), Mapping):
        yield 0, lambda payload: [payload], items
    for index, item in enumerate(schema.get("prefixItems", ())):
        yield index, lambda payload, index=index: [*([None] * index), payload], item
    if isinstance(values := schema.get("additionalProperties"), Mapping):
        yield "key", lambda payload: {"key": payload}, values


def _declared_open(core: Any) -> set[frozenset[str]]:
    """The fields of each type in a command's core schema whose own config allows any other key."""

    found: set[frozenset[str]] = set()
    pending = [core]
    while pending:
        value = pending.pop()
        if isinstance(value, Mapping):
            cls = value.get("cls")
            config = getattr(cls, "model_config", None) or getattr(cls, "__pydantic_config__", None) or {}
            if config.get("extra") == "allow":
                found.add(frozenset(json_schema(cls).get("properties", {})))
            pending.extend(value.values())
        elif isinstance(value, list | tuple):
            pending.extend(value)
    return found


def _refused_at(errors: list[dict[str, Any]], path: tuple[Any, ...]) -> bool:
    """Whether an unknown-field refusal names ``path``'s fields, in order, then the unknown one."""

    wanted = [part for part in path if isinstance(part, str)]
    for error in errors:
        loc = error["loc"]
        if error["type"] != "extra_forbidden" or not loc or loc[-1] != _UNKNOWN:
            continue
        reached = iter(loc[:-1])
        if all(part in reached for part in wanted):
            return True
    return False


class ClosedCommandTests(SimpleTestCase):
    def test_every_command_refuses_an_unknown_field_at_every_depth(self):
        open_levels = []
        for name, spec in sorted(capability_registry().items()):
            schema = capability_schema(spec)
            declared_open = _declared_open(TypeAdapter(spec.command_type).core_schema)
            for path, payload, node in _objects(schema, schema.get("$defs", {})):
                if frozenset(node["properties"]) in declared_open:
                    continue
                where = f"{name} at {'.'.join(map(str, path)) or 'the top'}"
                if node.get("additionalProperties") is not False:
                    open_levels.append(f"{where}: its schema is open")
                try:
                    validate_command(spec, payload)
                except ValidationError as exc:
                    details = pydantic_refusal(name, exc.errors()).details
                    if _refused_at(details, path):
                        continue
                open_levels.append(f"{where}: an unknown field is not refused")
        self.assertEqual(
            open_levels,
            [],
            "A command's types refuse a field they do not have. A host dataclass or TypedDict is closed by "
            "integration_specs.command_adapter; a Pydantic model keeps its own config, so an extension's is a "
            'StrictCommand. A type meant to stay open declares extra="allow" in its own config, with the '
            "reason beside it.",
        )


class RetiredNestedFieldTests(TestCase):
    def test_a_retired_field_inside_a_policy_cell_is_refused_by_its_path(self):
        person = get_user_model().objects.create_user("example-operator")
        cell = {"subject": "example-agent", "capability": "project.create", "rule": "allow", "scope": "all"}

        result = execute_capability(SET_POLICY, keyed({"rules": [cell]}), principal=web_principal(person))

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(
            [(detail["type"], detail["loc"]) for detail in result["error"]["details"]],
            [("extra_forbidden", ["rules", 0, "scope"])],
        )
        self.assertEqual(result["error"]["message"], f"{SET_POLICY}: rules[0].scope is not a known field.")
        self.assertFalse(CapabilityRule.objects.exists())
