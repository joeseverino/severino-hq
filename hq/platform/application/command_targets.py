"""Authorized target choices derived from the canonical resource registry."""

from dataclasses import dataclass
from typing import Any

from hq.platform.core.errors import UpstreamUnavailable

from .capabilities import CapabilitySpec
from .entity_links import kind_label
from .integrations import integration_graph
from .projection import MAX_PAGE_SIZE
from .resources import ResourceError, get_resource, list_resource
from .security import Principal


@dataclass(frozen=True, slots=True)
class CommandTargetOption:
    value: str
    label: str
    # What type of thing it is, in the words its own pages use; a long list
    # is offered under these.
    group: str = ""


def _option(item: dict[str, Any], value: str) -> CommandTargetOption:
    label = value
    for field in ("name", "title", "label"):
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate.strip():
            label = (
                f"{candidate.strip()} · {value}"
                if candidate.strip() != value
                else value
            )
            break
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
    resource = integration_graph().resources.get(spec.subject_resource)
    if not resource or not resource.list_handler or not resource.identifier:
        return None

    query = dict(spec.target_query)
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
    except UpstreamUnavailable:
        # The source is down or not configured: the target is typed instead of
        # chosen, rather than the page failing.
        return None
    options = []
    for item in collection["items"]:
        if not isinstance(item, dict):
            continue
        if (
            governed_kinds
            and not kinds_applied
            and item.get("kind") not in governed_kinds
        ):
            continue
        raw_value = item.get(resource.identifier)
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
    except ResourceError:  # an unknown or malformed target is not offered
        return options
    item = detail.get("resource", detail)
    if not isinstance(item, dict):
        return options
    wanted = dict(spec.target_query)
    if any(item.get(field) != value for field, value in wanted.items()):
        return options
    return (_option(item, target), *options)


def capability_target_initial(
    spec: CapabilitySpec, target: str, *, principal: Principal
) -> dict[str, Any]:
    """Hydrate declared command fields from one authorized local target."""

    if not spec.subject_resource or not spec.target_initial_fields:
        return {}
    detail = get_resource(spec.subject_resource, target, principal=principal)
    source = detail.get("resource", detail)
    if not isinstance(source, dict):
        return {}
    initial = {
        field: source[field] for field in spec.target_initial_fields if field in source
    }
    updated_at = source.get("updated_at")
    if isinstance(updated_at, str):
        initial["__expected_updated_at"] = updated_at
    return initial
