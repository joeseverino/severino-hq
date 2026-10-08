"""What the registry states, as the schemas the controller is generated from.

The registry declares each fact once: the kinds, the connection providers, why
a read fails, the parts a reading is read in, each reading's record, the values
both sides check (``SHARED`` on the module that owns them) and the shapes a
connection arrives in. This module turns those declarations into component
schemas. ``bridge_contract`` joins them to the bridge's own messages, and
``manage.py bridge_contract`` writes the result for the controller's generator.

Nothing here is read back: a schema is built from the declaration every time.
"""

import re
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import TypeAdapter

from .connection_shapes import SHAPES
from .observations import _SPEC_MODULES, OBSERVATIONS
from .provider_adapters import ADMITTED, CONNECTIONS
from .provider_adapters.contracts import FAILURES, REFUSALS
from .provider_spec import ConnectionShape, SharedValue
from .providers import OBSERVATION_KINDS, PROVIDERS
from .reading_parts import parts_of

REFERENCE = "#/components/schemas/"

# How a word of a registry name is written in a controller constant, where
# capitalising its first letter is not it.
_WORDS = {
    "acl": "ACL",
    "acme": "ACME",
    "adguard": "AdGuard",
    "api": "API",
    "cpanel": "CPanel",
    "d1": "D1",
    "dns": "DNS",
    "github": "GitHub",
    "hq": "HQ",
    "https": "HTTPS",
    "id": "ID",
    "npm": "NPM",
    "oauth": "OAuth",
    "onepassword": "OnePassword",
    "pki": "PKI",
    "querylog": "QueryLog",
    "ssh": "SSH",
    "tls": "TLS",
    "url": "URL",
}


def go_name(value: str) -> str:
    """A registry name as the controller spells it: ``npm.proxy_host`` is
    ``NPMProxyHost``.

    A pure function of the name and ``_WORDS``, so a name the registry keeps
    keeps its constant. Changing a word here renames every constant that has
    it, and the controller then fails to build wherever it used the old one.
    """

    return "".join(_WORDS.get(word, word[:1].upper() + word[1:]) for word in re.split(r"[._]", value.lower()) if word)


def _enumeration(description: str, values: Iterable[str], prefix: str, *, blank: str = "") -> dict[str, Any]:
    """A closed list of names, with the controller's constant for each."""

    listed = list(values)
    return {
        "type": "string",
        "description": description,
        "enum": listed,
        "x-enum-varnames": [(prefix + go_name(value)) if value else blank for value in listed],
    }


def swept_kinds() -> list[str]:
    """Every kind a controller reads in a sweep: a reading it takes, and a
    resource kind that does not say why nothing reads it."""

    read = {kind for kind, spec in OBSERVATIONS.items() if spec.read_by == "controller"}
    resources = {kind for kind, spec in PROVIDERS.items() if not spec.unobserved_reason}
    return sorted(read | resources)


def _enumerations() -> dict[str, dict[str, Any]]:
    kinds = sorted(set(PROVIDERS) | set(OBSERVATION_KINDS))
    parts = sorted({part for kind in kinds for part in parts_of(kind)})
    return {
        "ResourceKind": _enumeration(
            "Every kind of resource or reading HQ's provider registry declares.",
            kinds,
            "ResourceKind",
        ),
        "SweptKind": _enumeration(
            "A kind the controller reads in a sweep: exactly the kinds it registers a reader for.",
            swept_kinds(),
            "Swept",
        ),
        "Refusal": _enumeration(
            "Why a provider refused a read: the credential itself, or one permission "
            "it lacks. Empty when the reason is unclassified.",
            ("", *REFUSALS),
            "Refusal",
            blank="RefusalUnclassified",
        ),
        "FailureClass": _enumeration(
            "Why a read got no usable answer: a refusal, an address that answered as "
            "something other than the API, or nothing answering. Empty when "
            "unclassified.",
            ("", *FAILURES),
            "FailureClass",
            blank="FailureClassUnclassified",
        ),
        "ConnectionProvider": _enumeration(
            "A connection provider HQ's registry declares: what a credential is for.",
            sorted(CONNECTIONS),
            "ConnectionProvider",
        ),
        "ReadingPartName": _enumeration(
            "A part a reading is read in, as HQ's registry declares them; empty for "
            "the whole reading. HQ stores a refusal only for a part the kind declares.",
            parts,
            "Part",
            blank="PartWhole",
        ),
    }


def _nullable(members: list[dict[str, Any]]) -> dict[str, Any] | None:
    """``X | null`` as one schema with a type list, where X states one type."""

    others = [member for member in members if member != {"type": "null"}]
    if len(others) != 1 or len(members) != 2 or not isinstance(others[0].get("type"), str):
        return None
    return {**others[0], "type": [others[0]["type"], "null"]}


def _clean(node: Any, rename: Mapping[str, str]) -> Any:
    """A pydantic schema as the contract carries it.

    Titles are a form's, not the wire's. A nullable value states its type as a
    list, which the controller's generator reads as one type. A reference
    follows ``rename`` to where its target is filed.
    """

    if isinstance(node, list):
        return [_clean(item, rename) for item in node]
    if not isinstance(node, dict):
        return node
    found = {key: _clean(value, rename) for key, value in node.items() if key != "title" or not isinstance(value, str)}
    reference = found.get("$ref")
    if isinstance(reference, str):
        found["$ref"] = REFERENCE + rename[reference.removeprefix(REFERENCE)]
    if "anyOf" in found:
        nullable = _nullable(found["anyOf"])
        if nullable is not None:
            del found["anyOf"]
            found = {**nullable, **found}
    return found


def components(
    name: str,
    schema: Any,
    *,
    mode: str = "validation",
    description: str = "",
    owner: str = "",
) -> dict[str, dict[str, Any]]:
    """One type as component schemas: itself under ``name``, and each type it
    is built from under the owner's name followed by its own."""

    raw = TypeAdapter(schema).json_schema(mode=mode, ref_template=REFERENCE + "{model}")  # type: ignore[arg-type]
    nested = raw.pop("$defs", {})
    prefix = owner or name
    rename = {inner: prefix + inner for inner in nested}
    found = {name: _clean(raw, rename)}
    if description:
        found[name]["description"] = description
    for inner, definition in nested.items():
        found[rename[inner]] = _clean(definition, rename)
    if mode == "serialization":
        # What HQ sends is a validated model written out: every field is there.
        for schema in found.values():
            if "properties" in schema:
                schema["required"] = list(schema["properties"])
    return found


def _shared(value: SharedValue) -> dict[str, dict[str, Any]]:
    if isinstance(value.schema, tuple):
        return {
            value.name: {
                "type": "string",
                "description": value.description,
                "enum": list(value.schema),
                "x-enum-varnames": list(value.varnames) or [value.name + go_name(item) for item in value.schema],
            }
        }
    found = components(value.name, value.schema, mode=value.mode, description=value.description)
    for field, target in value.refs:
        found[value.name]["properties"][field] = {"$ref": REFERENCE + target}
    return found


def record_name(kind: str) -> str:
    """The component a reading's record is filed under."""

    return go_name(kind) + "Record"


def _records() -> Iterable[dict[str, dict[str, Any]]]:
    """Every record a controller reports, from the model HQ validates it with."""

    for kind, spec in OBSERVATIONS.items():
        if spec.read_by != "controller":
            continue
        name = record_name(kind)
        described = " ".join((spec.record.__doc__ or "").split())
        found = components(name, spec.record, owner=name.removesuffix("Record"))
        found[name]["description"] = described or f"One {kind} record: {spec.label}."
        yield found


def _declared() -> Iterable[dict[str, dict[str, Any]]]:
    yield _enumerations()
    for module in (*ADMITTED, *_SPEC_MODULES):
        for value in getattr(module, "SHARED", ()):
            yield _shared(value)
    yield from _records()


def schemas() -> dict[str, dict[str, Any]]:
    """Every component schema the registry owns, by name.

    Two declarations under one name are an error, whichever came first.
    """

    found: dict[str, dict[str, Any]] = {}
    for group in _declared():
        for name, schema in group.items():
            if name in found:
                raise ValueError(f"Two declarations emit the bridge schema {name!r}.")
            found[name] = schema
    return dict(sorted(found.items()))


# ---------------------------------------------------------------- connections

CONNECTIONS_TITLE = "HQ controller connections"


def setting_key(name: str) -> str:
    """A setting's member name in the connections document."""

    return name.lower()


def _shape(shape: ConnectionShape) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for setting in shape.settings:
        tags = {"setting": setting.name}
        if setting.secret:
            tags["secret"] = "true"
        properties[setting_key(setting.name)] = {
            "type": "string",
            "x-go-name": go_name(setting.name),
            "x-oapi-codegen-extra-tags": tags,
        }
    return {
        "type": "object",
        "description": f"The settings a connection of the {shape.name} shape arrives with.",
        "properties": properties,
        "required": [setting_key(setting.name) for setting in shape.settings if not setting.optional],
        "additionalProperties": False,
    }


def connections_document() -> dict[str, Any]:
    """The schema of the document that carries connections to the controller.

    The renderer writes the document and the controller reads it, both through
    the types generated from this. A connection states who it is for and holds
    its settings under the one shape it arrived in.
    """

    shapes = {go_name(name): shape for name, shape in SHAPES.items()}
    members = {
        shape.name: {
            "$ref": REFERENCE + name,
            "x-go-name": name,
            "x-go-type-skip-optional-pointer": False,
        }
        for name, shape in shapes.items()
    }
    connection = {
        "type": "object",
        "description": (
            "One connection: its reference, the provider it is for, whether HQ may "
            "change things through it, where its credential is kept, and its settings "
            "under the one shape it arrived in."
        ),
        "properties": {
            "ref": {"type": "string"},
            "provider": {"type": "string"},
            "manages": {"type": "boolean"},
            "store": {"$ref": REFERENCE + "Store"},
            **members,
        },
        "required": ["ref", "provider", "manages", "store"],
        "additionalProperties": False,
    }
    store = {
        "type": "object",
        "description": (
            "Where a connection's credential is kept, as references only: the vault, "
            "the item, and the bootstrap item a replacement is minted with."
        ),
        "properties": {
            "vault": {"type": "string"},
            "item": {"type": "string"},
            "bootstrap": {"type": "string"},
        },
        "required": ["vault", "item"],
        "additionalProperties": False,
    }
    document = {
        "type": "object",
        "description": "Every connection the controller may open.",
        "properties": {
            "schema_version": {"type": "integer"},
            "connections": {
                "type": "array",
                "items": {"$ref": REFERENCE + "Connection"},
            },
        },
        "required": ["schema_version", "connections"],
        "additionalProperties": False,
    }
    return {
        "openapi": "3.2.0",
        "info": {
            "title": CONNECTIONS_TITLE,
            "version": "2.0.0",
            "description": (
                "The document that carries provider connections from the secret "
                "renderer to the controller. It is a file, not an API: there are no "
                "paths. Emitted from the connection shapes HQ's registry declares; "
                "the Go types both programs use are generated from it."
            ),
        },
        "paths": {},
        "components": {
            "schemas": {
                "Connection": connection,
                "Document": document,
                "Store": store,
                **{name: _shape(shape) for name, shape in sorted(shapes.items())},
            }
        },
    }
