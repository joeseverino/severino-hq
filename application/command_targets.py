"""Authorized target choices derived from the canonical resource registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .capabilities import CapabilitySpec, capability_label
from .integrations import integration_graph
from .resources import get_resource, list_resource
from .security import Principal


@dataclass(frozen=True)
class CommandTargetOption:
    value: str
    label: str


def _option_label(item: dict[str, Any], value: str) -> str:
    base = value
    for field in ("name", "title", "label"):
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate.strip():
            base = (
                f"{candidate.strip()} · {value}"
                if candidate.strip() != value
                else value
            )
            break
    kind = item.get("kind")
    if isinstance(kind, str) and kind.strip():
        return f"{base} · {capability_label(kind)}"
    return base


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
    kinds_applied = False
    if governed_kinds:
        query_fields = resource.list_query_type.model_fields
        if "kinds" in query_fields and "kind" not in query:
            query["kinds"] = ",".join(governed_kinds)
            kinds_applied = True
        elif len(governed_kinds) == 1 and "kind" in query_fields:
            query["kind"] = governed_kinds[0]
            kinds_applied = True

    collection = list_resource(
        resource.name,
        query,
        principal=principal,
    )
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
        options.append(CommandTargetOption(value, _option_label(item, value)))
    return tuple(sorted(options, key=lambda option: option.label.casefold()))


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
