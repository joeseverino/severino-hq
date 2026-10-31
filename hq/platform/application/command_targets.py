"""Authorized target choices derived from the canonical resource registry."""

from dataclasses import dataclass
from typing import Any

lazy from django.apps import apps

from hq.platform.core.errors import UpstreamUnavailable

from .capabilities import CapabilitySpec
from .command_fields import named_records, referred
from .contracts import route_url
from .entity_links import kind_label
from .integration_specs import ON_ITS_PAGE
from .integrations import integration_graph
from .projection import MAX_PAGE_SIZE
from .record_errors import RecordNotFound
from .resources import ResourceError, get_resource, list_resource
from .security import AuthorizationError, Principal
lazy from . import references
lazy from .domains import domain_navigation


@dataclass(frozen=True, slots=True)
class CommandTargetOption:
    value: str
    label: str
    # What type of thing it is, in the words its own pages use; a long list
    # is offered under these.
    group: str = ""


def item_name(item: dict[str, Any]) -> str:
    """What a record read says it is called, or "" where it names nothing."""

    for field in ("name", "title", "label", "item_name", "item"):
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _option(item: dict[str, Any], value: str) -> CommandTargetOption:
    name = item_name(item)
    label = f"{name} · {value}" if name and name != value else value
    kind = item.get("kind")
    group = kind_label(kind) if isinstance(kind, str) and kind.strip() else ""
    return CommandTargetOption(value, label, group)


def capability_target_options(
    spec: CapabilitySpec,
    *,
    principal: Principal,
    governed_kinds: tuple[str, ...] = (),
) -> tuple[CommandTargetOption, ...] | None:
    """List local authorized targets, or ``None`` when the spec is not listable."""

    if not spec.target_kind or not spec.subject_resource:
        return None
    return resource_options(
        spec.subject_resource,
        principal=principal,
        query=dict(spec.target_query),
        governed_kinds=governed_kinds,
    )


def starting_page(spec: CapabilitySpec) -> tuple[str, str] | None:
    """``(label, url)`` of the page a person starts a ``reached_from``
    command from, called what the navigation or its records call it, or
    ``None`` for one the page of whatever it acts on sends."""

    route = spec.reached_from
    if not route or route == ON_ITS_PAGE:
        return None
    named = (
        *(item.label for item in domain_navigation() if item.route == route),
        *(resource.label for resource in integration_graph().resources.values() if resource.web_route == route),
    )
    return (named[0] if named else "", route_url(route))


def record_options(
    schema: dict[str, Any], *, principal: Principal
) -> dict[str, tuple[CommandTargetOption, ...] | None]:
    """The records each record-naming field of a command offers, by field.

    A field whose source cannot be listed maps to ``None``, so the form asks
    for the value typed rather than failing the page.
    """

    offered: dict[str, tuple[CommandTargetOption, ...] | None] = {}
    for name, field in schema.get("properties", {}).items():
        declared = named_records(field)
        if declared is not None:
            offered[name] = resource_options(declared["resource"], principal=principal, key=declared.get("key", ""))
        elif (reference := referred(field)) is not None:
            offered[name] = reference_options(*reference, principal=principal)
    return offered


def reference_options(model: str, field: str, *, principal: Principal) -> tuple[CommandTargetOption, ...]:
    """What one model's reference column offers this caller, under the
    headings the record's own form shows: ``references.choices`` is the one
    list, so the two forms cannot disagree."""

    column = apps.get_model(model)._meta.get_field(field)
    return tuple(
        CommandTargetOption(value, label, heading)
        for heading, group in references.choices(column, principal=principal)
        if isinstance(group, list)
        for value, label in group
    )


def resource_options(
    resource_name: str,
    *,
    principal: Principal,
    query: dict[str, Any] | None = None,
    key: str = "",
    governed_kinds: tuple[str, ...] = (),
) -> tuple[CommandTargetOption, ...] | None:
    """Every record of one resource the caller may read, as choices by ``key``
    (the resource's identifier unless named), or ``None`` when it is not
    listable."""

    resource = integration_graph().resources.get(resource_name)
    key = key or (resource.identifier if resource else "") or ""
    if not resource or not resource.list_handler or not key:
        return None

    query = dict(query or {})
    # A choice list is the whole catalog: a default-sized page would leave
    # every resource past it unselectable.
    if "limit" in resource.list_query_type.model_fields:
        query.setdefault("limit", MAX_PAGE_SIZE)
    kinds_applied = False
    if governed_kinds:
        query_fields = resource.list_query_type.model_fields
        if "kinds" in query_fields and "kind" not in query:
            query["kinds"] = ",".join(governed_kinds)
            kinds_applied = True
        elif len(governed_kinds) == 1 and "kind" in query_fields:
            query["kind"] = governed_kinds[0]
            kinds_applied = True

    try:
        collection = list_resource(resource.name, query, principal=principal)
    except UpstreamUnavailable, AuthorizationError:
        # The source is down, not configured or not the caller's to read: the
        # value is typed instead of chosen, rather than the page failing.
        return None
    options = []
    for item in collection["items"]:
        if not isinstance(item, dict):
            continue
        if governed_kinds and not kinds_applied and item.get("kind") not in governed_kinds:
            continue
        raw_value = item.get(key)
        if raw_value is None:
            continue
        value = str(raw_value)
        options.append(_option(item, value))
    return tuple(sorted(options, key=lambda option: option.label.casefold()))


def with_requested_target(
    spec: CapabilitySpec,
    options: tuple[CommandTargetOption, ...],
    target: str,
    *,
    principal: Principal,
) -> tuple[CommandTargetOption, ...]:
    """The choices, holding the one a link asked for even past the listed page.

    A remedy links here with its target, and a catalog larger than one page
    would otherwise drop it. It is added only when the caller may read it and
    it is the kind this command acts on, so a link cannot widen the choice.
    """

    if not target or any(option.value == target for option in options):
        return options
    if not spec.subject_resource:
        return options
    try:
        detail = get_resource(spec.subject_resource, target, principal=principal)
    except ResourceError, RecordNotFound:  # an unknown, malformed or missing target is not offered
        return options
    item = detail.get("resource", detail)
    if not isinstance(item, dict):
        return options
    wanted = dict(spec.target_query)
    if any(item.get(field) != value for field, value in wanted.items()):
        return options
    return (_option(item, target), *options)


def capability_target_initial(spec: CapabilitySpec, target: str, *, principal: Principal) -> dict[str, Any]:
    """Hydrate declared command fields from one authorized local target."""

    if not spec.subject_resource or not spec.target_initial_fields:
        return {}
    detail = get_resource(spec.subject_resource, target, principal=principal)
    source = detail.get("resource", detail)
    if not isinstance(source, dict):
        return {}
    initial = {field: source[field] for field in spec.target_initial_fields if field in source}
    updated_at = source.get("updated_at")
    if isinstance(updated_at, str):
        initial["__expected_updated_at"] = updated_at
    return initial
