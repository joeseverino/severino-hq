"""Intrinsic validation for every contract compiled into IntegrationGraph.

Emitters only emit. The graph compiler calls this module, so no caller can
assemble a registry while accidentally bypassing the same contract checks the
runtime composition receives.
"""

from __future__ import annotations

import inspect
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, ValidationError

from .connection_contracts import GRANT_MODELS, ConnectionAbility, ConnectionSpec
from .contracts import DJANGO_ROUTE, DOTTED_NAME, EFFECTS, SCOPE_NAME
from .integration_specs import (
    TARGET_KINDS,
    CapabilitySpec,
    ResourceSpec,
    command_schema,
)
from .search_contracts import SearchDefinition
from .security import Capability


IntegrationSpec = CapabilitySpec | ResourceSpec | ConnectionSpec


def required_capability_names(spec: IntegrationSpec) -> tuple[str, ...]:
    return tuple(
        item.value if isinstance(item, Capability) else item
        for item in spec.required_capabilities
    )


def safe_connection_url(url: str) -> bool:
    """Allow explicit web URLs and local paths, never executable schemes."""

    return url.startswith(("http://", "https://")) or (
        url.startswith("/") and not url.startswith("//")
    )


def validate_capability_spec(spec: CapabilitySpec) -> None:
    if not DOTTED_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid capability name {spec.name!r}.")
    if not spec.summary.strip():
        raise ImproperlyConfigured(f"Capability {spec.name!r} has no summary.")
    if spec.effect not in EFFECTS:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid effect {spec.effect!r}."
        )
    if spec.target_kind is not None and spec.target_kind not in TARGET_KINDS:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid target {spec.target_kind!r}."
        )
    if not isinstance(spec.target_label, str) or not isinstance(spec.target_help, str):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid target presentation metadata."
        )
    if spec.target_query and (not spec.target_kind or not spec.subject_resource):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has a target query without a target resource."
        )
    if any(
        not isinstance(item, tuple) or len(item) != 2 or not isinstance(item[0], str)
        for item in spec.target_query
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has an invalid target query."
        )
    if any(
        not isinstance(note, str) or not note.strip() for note in spec.execution_notes
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has an invalid execution note."
        )
    try:
        schema = command_schema(spec.command_type)
    except Exception as exc:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} command type cannot emit JSON Schema."
        ) from exc
    command_fields = set(schema.get("properties", {}))
    if (
        len(spec.target_initial_fields) != len(set(spec.target_initial_fields))
        or any(field not in command_fields for field in spec.target_initial_fields)
        or (spec.target_initial_fields and not spec.target_kind)
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid target initial fields."
        )
    if spec.subject_resource is not None and not DOTTED_NAME.fullmatch(
        spec.subject_resource
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} has invalid resource {spec.subject_resource!r}."
        )
    required = required_capability_names(spec)
    if not required or any(
        not isinstance(item, str) or not DOTTED_NAME.fullmatch(item)
        for item in required
    ):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} must declare valid required capabilities."
        )
    if len(required) != len(set(required)):
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} repeats a required capability."
        )
    if not callable(spec.handler):
        raise ImproperlyConfigured(f"Capability {spec.name!r} handler is not callable.")

    kwargs: dict[str, Any] = {"principal": None, "expected_updated_at": None}
    kind = TARGET_KINDS.get(spec.target_kind) if spec.target_kind else None
    if kind:
        kwargs[kind.keyword] = None
    try:
        inspect.signature(spec.handler).bind(None, **kwargs)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"Capability {spec.name!r} handler does not implement the host call contract."
        ) from exc


def _validate_resource_identity(spec: ResourceSpec) -> None:
    if not DOTTED_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid resource name {spec.name!r}.")
    if not spec.label.strip() or not spec.summary.strip():
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare a label and summary."
        )
    required = required_capability_names(spec)
    if (
        not required
        or len(required) != len(set(required))
        or any(not DOTTED_NAME.fullmatch(item) for item in required)
    ):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare unique valid capabilities."
        )


def _validate_list_contract(spec: ResourceSpec) -> None:
    if bool(spec.list_handler) != bool(spec.list_query_type):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare its list handler and query together."
        )
    if spec.list_query_type and not (
        inspect.isclass(spec.list_query_type)
        and issubclass(spec.list_query_type, BaseModel)
        and callable(spec.list_handler)
    ):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has an invalid list contract."
        )
    if spec.list_handler and spec.list_query_type:
        try:
            values = spec.list_query_type().model_dump()
            inspect.signature(spec.list_handler).bind(**values)
        except (TypeError, ValidationError) as exc:
            raise ImproperlyConfigured(
                f"Resource {spec.name!r} list handler does not implement its query contract."
            ) from exc


def _validate_detail_contract(spec: ResourceSpec) -> None:
    if bool(spec.detail_handler) != bool(spec.identifier):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} must declare its detail handler and identifier together."
        )
    if not spec.detail_handler:
        return
    try:
        inspect.signature(spec.detail_handler).bind(None)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} detail handler does not accept one identifier."
        ) from exc
    if not isinstance(spec.identifier_type, type):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has an invalid identifier type."
        )
    if any(
        not isinstance(error, type) or not issubclass(error, Exception)
        for error in spec.not_found_errors
    ):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has invalid not-found errors."
        )


def _validate_search_contract(spec: ResourceSpec) -> None:
    if not any((spec.list_handler, spec.detail_handler, spec.search)):
        raise ImproperlyConfigured(f"Resource {spec.name!r} exposes no operations.")
    if spec.search and not isinstance(spec.search, SearchDefinition):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has an invalid search definition."
        )
    if spec.search and spec.search.scope != spec.name:
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} search scope must use the same name."
        )
    if spec.web_route and not DJANGO_ROUTE.fullmatch(spec.web_route):
        raise ImproperlyConfigured(
            f"Resource {spec.name!r} has invalid web route {spec.web_route!r}."
        )


def validate_resource_spec(spec: ResourceSpec) -> None:
    _validate_resource_identity(spec)
    _validate_list_contract(spec)
    _validate_detail_contract(spec)
    _validate_search_contract(spec)


def _validate_ability(spec: ConnectionSpec, ability: ConnectionAbility) -> None:
    if not isinstance(ability, ConnectionAbility):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} returned a non-ConnectionAbility."
        )
    if not DOTTED_NAME.fullmatch(ability.name):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} has invalid ability {ability.name!r}."
        )
    if not ability.label.strip() or not ability.summary.strip():
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} needs a label and summary."
        )
    if ability.effect not in EFFECTS:
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid effect {ability.effect!r}."
        )
    if len(ability.required_scopes) != len(set(ability.required_scopes)) or any(
        not SCOPE_NAME.fullmatch(scope) for scope in ability.required_scopes
    ):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid required scopes."
        )
    if ability.grant not in ("", *GRANT_MODELS):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid grant model "
            f"{ability.grant!r}."
        )
    if ability.grant == "scoped" and not ability.required_scopes:
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} declares scoped proof but "
            "requires no scopes."
        )
    if ability.grant in ("coarse", "none") and ability.required_scopes:
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} declares {ability.grant} proof "
            "but lists required scopes."
        )
    if ability.capability and not DOTTED_NAME.fullmatch(ability.capability):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid capability."
        )
    if len(ability.governs_kinds) != len(set(ability.governs_kinds)) or any(
        not DOTTED_NAME.fullmatch(kind) for kind in ability.governs_kinds
    ):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has invalid governed kinds."
        )
    if ability.subject_resource and not DOTTED_NAME.fullmatch(ability.subject_resource):
        raise ImproperlyConfigured(
            f"Connection ability {ability.name!r} has an invalid subject resource."
        )


def validate_connection_spec(spec: ConnectionSpec) -> None:
    if not DOTTED_NAME.fullmatch(spec.name):
        raise ImproperlyConfigured(f"Invalid connection name {spec.name!r}.")
    if not spec.label.strip() or not spec.summary.strip():
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} needs a label and summary."
        )
    required = required_capability_names(spec)
    if (
        not required
        or len(required) != len(set(required))
        or any(not DOTTED_NAME.fullmatch(item) for item in required)
    ):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} must declare unique valid capabilities."
        )
    try:
        inspect.signature(spec.instance_provider).bind()
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} instance provider must take no arguments."
        ) from exc
    for route in (spec.web_route, spec.management_route, spec.setup_route):
        if route and not DJANGO_ROUTE.fullmatch(route):
            raise ImproperlyConfigured(
                f"Connection {spec.name!r} has invalid route {route!r}."
            )
    if spec.documentation_url and not safe_connection_url(spec.documentation_url):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} has an invalid documentation URL."
        )
    if (
        isinstance(spec.stale_after_hours, bool)
        or not isinstance(spec.stale_after_hours, int)
        or spec.stale_after_hours <= 0
    ):
        raise ImproperlyConfigured(
            f"Connection {spec.name!r} must declare a positive staleness window."
        )
    for ability in spec.abilities:
        _validate_ability(spec, ability)
    names = [ability.name for ability in spec.abilities]
    if len(names) != len(set(names)):
        raise ImproperlyConfigured(f"Connection {spec.name!r} repeats an ability name.")
