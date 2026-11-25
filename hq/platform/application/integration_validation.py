"""Intrinsic validation for every contract compiled into IntegrationGraph.

Emitters only emit. The graph compiler calls this module, so no caller can
assemble a registry while accidentally bypassing the same contract checks the
runtime composition receives.
"""

import inspect
import re
from annotationlib import Format
from typing import Any, is_typeddict

from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, PydanticUndefinedAnnotation, PydanticUserError, ValidationError

from .connection_contracts import ConnectionAbility, ConnectionSpec
from .contracts import DJANGO_ROUTE, DOTTED_NAME, SCOPE_NAME
from .integration_specs import (
    CONSENT_KEYWORD,
    RESERVED_QUERY_FIELD,
    SENT_KEYWORD,
    STALE_WRITE_KEYWORD,
    TARGET,
    TARGET_KINDS,
    CapabilitySpec,
    ResourceSpec,
    command_schema,
    takes_consent,
    takes_sent_fields,
    takes_stale_write_token,
    typed_keys,
)
from .search_contracts import SearchDefinition
from .security import Capability
lazy from .entity_links import link_url

IntegrationSpec = CapabilitySpec | ResourceSpec | ConnectionSpec

# The API's names (docs/API.md#names): a capability is
# <area>[.<noun>...].<verb>, a resource [<owner>.]<noun>, each in dotted
# snake_case.
_SEGMENT = r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*"
_CAPABILITY_NAME = re.compile(rf"{_SEGMENT}(?:\.{_SEGMENT})+")
_RESOURCE_NAME = re.compile(rf"(?:{_SEGMENT}\.)?{_SEGMENT}")
CAPABILITY_PATTERN = "<area>[.<noun>...].<verb>"
RESOURCE_PATTERN = "[<owner>.]<noun>"

# Every verb a capability's last segment may be. A new one is added here on
# purpose, as a word of the API, rather than coined by a name.
VERBS = frozenset(
    {
        "accept_observed",
        "adopt",
        "allow",
        "apply",
        "approve",
        "cancel",
        "choose",
        "close",
        "complete",
        "create",
        "decide",
        "delete",
        "import",
        "list",
        "move",
        "open",
        "read",
        "reconcile",
        "reconnect",
        "refresh",
        "refresh_due",
        "reject",
        "remove",
        "remove_empty_groups",
        "renew",
        "replace",
        "restart",
        "restore",
        "review",
        "run",
        "select",
        "set",
        "set_aside",
        "sign_out",
        "start",
        "stop",
        "sync",
        "toggle",
        "update",
        "upload",
        "withdraw",
    }
)

# Every resource is one, and the API serves it under /resources/ already.
_STUTTER = frozenset({"resource", "resources"})


def capability_name_refusal(name: str) -> str | None:
    """Why ``name`` is no capability name, or None for one that is."""

    if not _CAPABILITY_NAME.fullmatch(name):
        return f"{name!r} is not {CAPABILITY_PATTERN} in dotted snake_case."
    verb = name.rpartition(".")[2]
    if verb not in VERBS:
        return f"{name!r} does not end in a verb, as {CAPABILITY_PATTERN} does; {verb!r} is none of VERBS."
    return None


def resource_name_refusal(name: str) -> str | None:
    """Why ``name`` is no resource name, or None for one that is."""

    if not _RESOURCE_NAME.fullmatch(name):
        return f"{name!r} is not {RESOURCE_PATTERN} in dotted snake_case."
    if name.rpartition(".")[2] in _STUTTER:
        return f"{name!r} names its records 'resources', as the path it is served at already says ({RESOURCE_PATTERN})."
    return None


def required_capability_names(spec: IntegrationSpec) -> tuple[str, ...]:
    return tuple(item.value if isinstance(item, Capability) else item for item in spec.required_capabilities)


def validate_capability_spec(spec: CapabilitySpec) -> None:
    _validate_capability_identity(spec)
    _validate_capability_target(spec)
    _validate_capability_command(spec)
    _validate_capability_requirements(spec)
    _validate_capability_handler(spec)


def _validate_capability_identity(spec: CapabilitySpec) -> None:
    if why := capability_name_refusal(spec.name):
        raise ImproperlyConfigured(f"Invalid capability name: {why}")
    if not spec.summary.strip():
        raise ImproperlyConfigured(f"Capability {spec.name!r} has no summary.")
    words = (spec.label, spec.verb, *(text for _name, text in spec.variants))
    if any(text != text.strip() or len(text) > 60 for text in words):
        raise ImproperlyConfigured(f"Capability {spec.name!r} needs short words without padding.")
    names = [name for name, _text in spec.variants]
    if any(not name or not text for name, text in spec.variants) or len(names) != len(set(names)):
        raise ImproperlyConfigured(f"Capability {spec.name!r} names each variant once, with its words.")


def _validate_capability_target(spec: CapabilitySpec) -> None:
    if spec.target_kind is not None and spec.target_kind not in TARGET_KINDS:
        raise ImproperlyConfigured(f"Capability {spec.name!r} has invalid target {spec.target_kind!r}.")
    if spec.target_query and (not spec.target_kind or not spec.subject_resource):
        raise ImproperlyConfigured(f"Capability {spec.name!r} has a target query without a target resource.")
    if any(not note.strip() for note in spec.execution_notes):
        raise ImproperlyConfigured(f"Capability {spec.name!r} has an invalid execution note.")
    if spec.wants and (
        spec.wants != spec.wants.strip()
        or len(spec.wants) > 80
        or spec.wants.count(TARGET) != (1 if spec.target_kind else 0)
        or "{" in spec.wants.replace(TARGET, "")
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} says what it wants in a short phrase, with {TARGET} where its target goes "
            "exactly when it takes one."
        )


def _validate_capability_command(spec: CapabilitySpec) -> None:
    try:
        schema = command_schema(spec.command_type)
    except (PydanticUserError, PydanticUndefinedAnnotation) as exc:
        raise ImproperlyConfigured(f"Capability {spec.name!r} command type cannot emit JSON Schema.") from exc
    command_fields = set(schema.get("properties", {}))
    if (
        len(spec.target_initial_fields) != len(set(spec.target_initial_fields))
        or any(field not in command_fields for field in spec.target_initial_fields)
        or (spec.target_initial_fields and not spec.target_kind)
    ):
        raise ImproperlyConfigured(f"Capability {spec.name!r} has invalid target initial fields.")


def _validate_capability_requirements(spec: CapabilitySpec) -> None:
    if spec.subject_resource is not None and not DOTTED_NAME.fullmatch(spec.subject_resource):
        raise ImproperlyConfigured(f"Capability {spec.name!r} has invalid resource {spec.subject_resource!r}.")
    required = required_capability_names(spec)
    if not required or any(not DOTTED_NAME.fullmatch(item) for item in required):
        raise ImproperlyConfigured(f"Capability {spec.name!r} must declare valid required capabilities.")
    if len(required) != len(set(required)):
        raise ImproperlyConfigured(f"Capability {spec.name!r} repeats a required capability.")


def _validate_capability_handler(spec: CapabilitySpec) -> None:
    if not callable(spec.handler):
        raise ImproperlyConfigured(f"Capability {spec.name!r} handler is not callable.")
    # A handler that writes a declaration takes the consent in place of the principal.
    kwargs: dict[str, Any] = {CONSENT_KEYWORD if takes_consent(spec.handler) else "principal": None}
    if takes_stale_write_token(spec.handler):
        kwargs[STALE_WRITE_KEYWORD] = None
    if takes_sent_fields(spec.handler):
        kwargs[SENT_KEYWORD] = None
    kind = TARGET_KINDS.get(spec.target_kind) if spec.target_kind else None
    if kind:
        kwargs[kind.keyword] = None
    try:
        inspect.signature(spec.handler, annotation_format=Format.STRING).bind(None, **kwargs)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} handler does not implement the host call contract."
        ) from exc


def _validate_resource_identity(spec: ResourceSpec) -> None:
    if why := resource_name_refusal(spec.name):
        raise ImproperlyConfigured(f"Invalid resource name: {why}")
    if not spec.label.strip() or not spec.summary.strip():
        raise ImproperlyConfigured(f"Resource {spec.name!r} must declare a label and summary.")
    required = required_capability_names(spec)
    if not required or len(required) != len(set(required)) or any(not DOTTED_NAME.fullmatch(item) for item in required):
        raise ImproperlyConfigured(f"Resource {spec.name!r} must declare unique valid capabilities.")


def _principal_argument(spec: ResourceSpec) -> dict[str, None]:
    return {"principal": None} if spec.pass_principal else {}


def _query_names(query_type: type[BaseModel]) -> set[str]:
    """Every name a caller could send a query field by: its own and its aliases."""

    return {
        key
        for name, field in query_type.model_fields.items()
        for key in (name, field.alias, field.validation_alias)
        if isinstance(key, str)
    }


def _validate_list_contract(spec: ResourceSpec) -> None:
    if spec.listing is None:
        return
    read = spec.listing
    if read.query.model_config.get("extra") != "forbid":
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} query must refuse a filter it does not declare: base it on ResourceQuery."
        )
    if RESERVED_QUERY_FIELD in _query_names(read.query):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} query may not declare {RESERVED_QUERY_FIELD!r}: it selects the keys returned."
        )
    # An extension's declaration is read as it arrived, which its annotation does not promise.
    declared: tuple[object, ...] = read.fields
    if len(declared) != len(set(declared)) or any(
        not isinstance(field, str) or not field.isidentifier() for field in declared
    ):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} fields must be the unique top-level names of a list's items."
        )
    try:
        values = read.query().model_dump(exclude=spec.read_around)
        inspect.signature(read.reader, annotation_format=Format.STRING).bind(**values, **_principal_argument(spec))
    except (TypeError, ValidationError) as exc:
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} list handler does not implement its query contract."
        ) from exc


def _validate_detail_contract(spec: ResourceSpec) -> None:
    if bool(spec.detail_handler) != bool(spec.identifier):
        raise ImproperlyConfigured(f"Resource {spec.name!r} must declare its detail handler and identifier together.")
    if not spec.detail_handler:
        return
    try:
        inspect.signature(spec.detail_handler, annotation_format=Format.STRING).bind(None, **_principal_argument(spec))
    except TypeError as exc:
        raise ImproperlyConfigured(f"Resource {spec.name!r} detail handler does not accept one identifier.") from exc


def _validate_search_contract(spec: ResourceSpec) -> None:
    if not any((spec.listing, spec.detail_handler, spec.search)):
        raise ImproperlyConfigured(f"Resource {spec.name!r} exposes no operations.")
    if spec.search and not isinstance(spec.search, SearchDefinition):
        raise ImproperlyConfigured(f"Resource {spec.name!r} has an invalid search definition.")
    if spec.search and spec.search.scope != spec.name:
        raise ImproperlyConfigured(f"Resource {spec.name!r} search scope must use the same name.")
    if spec.web_route and not DJANGO_ROUTE.fullmatch(spec.web_route):
        raise ImproperlyConfigured(f"Resource {spec.name!r} has invalid web route {spec.web_route!r}.")


def _validate_record_types(spec: ResourceSpec) -> None:
    """A list's whole item and a read of one's record are each a TypedDict, so
    an answer holds only the keys its type declares; a list's default fields
    are some of its item's. A record usually extends the item, but a read of
    one may answer the item beside what it is read with (a service and its
    path), so that is not required."""

    typed: tuple[tuple[type, str], ...] = ((spec.listing.item, "list"),) if spec.listing else ()
    if spec.record_type is not None:
        if not spec.detail_handler:
            raise ImproperlyConfigured(f"Resource {spec.name!r} types a detail read it does not serve.")
        typed += ((spec.record_type, "detail"),)
    for kind, operation in typed:
        if not is_typeddict(kind):
            raise ImproperlyConfigured(f"Resource {spec.name!r} must type its {operation} read with a TypedDict.")
    if spec.listing and (undeclared := set(spec.listing.fields) - set(typed_keys(spec.listing.item))):
        raise ImproperlyConfigured(f"Resource {spec.name!r} fields {sorted(undeclared)} are not keys of its item.")


def validate_resource_spec(spec: ResourceSpec) -> None:
    _validate_resource_identity(spec)
    _validate_list_contract(spec)
    _validate_detail_contract(spec)
    _validate_record_types(spec)
    _validate_search_contract(spec)


def _validate_ability(spec: ConnectionSpec, ability: ConnectionAbility) -> None:
    if not isinstance(ability, ConnectionAbility):
        raise ImproperlyConfigured(f"Connection {spec.name!r} returned a non-ConnectionAbility.")
    if not DOTTED_NAME.fullmatch(ability.name):
        raise ImproperlyConfigured(f"Connection {spec.name!r} has invalid ability {ability.name!r}.")
    if not ability.label.strip() or not ability.summary.strip():
        raise ImproperlyConfigured(f"Connection ability {ability.name!r} needs a label and summary.")
    if len(ability.required_scopes) != len(set(ability.required_scopes)) or any(
        not SCOPE_NAME.fullmatch(scope) for scope in ability.required_scopes
    ):
        raise ImproperlyConfigured(f"Connection ability {ability.name!r} has invalid required scopes.")
    if ability.capability and not DOTTED_NAME.fullmatch(ability.capability):
        raise ImproperlyConfigured(f"Connection ability {ability.name!r} has invalid capability.")
    if len(ability.governs_kinds) != len(set(ability.governs_kinds)) or any(
        not DOTTED_NAME.fullmatch(kind) for kind in ability.governs_kinds
    ):
        raise ImproperlyConfigured(f"Connection ability {ability.name!r} has invalid governed kinds.")
    if ability.subject_resource and not DOTTED_NAME.fullmatch(ability.subject_resource):
        raise ImproperlyConfigured(f"Connection ability {ability.name!r} has an invalid subject resource.")


def validate_connection_spec(spec: ConnectionSpec) -> None:
    if not DOTTED_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid connection name {spec.name!r}.")
    if not spec.label.strip() or not spec.summary.strip():
        raise ImproperlyConfigured(f"Connection {spec.name!r} needs a label and summary.")
    required = required_capability_names(spec)
    if not required or len(required) != len(set(required)) or any(not DOTTED_NAME.fullmatch(item) for item in required):
        raise ImproperlyConfigured(f"Connection {spec.name!r} must declare unique valid capabilities.")
    try:
        inspect.signature(spec.instance_provider, annotation_format=Format.STRING).bind()
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(f"Connection {spec.name!r} instance provider must take no arguments.") from exc
    for route in (spec.web_route, spec.management_route, spec.setup_route):
        if route and not DJANGO_ROUTE.fullmatch(route):
            raise ImproperlyConfigured(f"Connection {spec.name!r} has invalid route {route!r}.")
    if spec.documentation_url and not link_url(spec.documentation_url):
        raise ImproperlyConfigured(f"Connection {spec.name!r} has an invalid documentation URL.")
    for ability in spec.abilities:
        _validate_ability(spec, ability)
    names = [ability.name for ability in spec.abilities]
    if len(names) != len(set(names)):
        raise ImproperlyConfigured(f"Connection {spec.name!r} repeats an ability name.")
