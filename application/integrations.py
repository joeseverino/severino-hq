"""Compile HQ's independently emitted contracts into one immutable graph."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping, TypeVar

from django.core.exceptions import ImproperlyConfigured

from .capabilities import CapabilitySpec, _collect_capabilities
from .connections import ConnectionSpec, _collect_connections
from .resources import ResourceSpec, _collect_resources


Spec = TypeVar("Spec", CapabilitySpec, ResourceSpec, ConnectionSpec)


@dataclass(frozen=True, slots=True)
class IntegrationGraph:
    capabilities: Mapping[str, CapabilitySpec]
    resources: Mapping[str, ResourceSpec]
    connections: Mapping[str, ConnectionSpec]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capabilities", MappingProxyType(dict(self.capabilities))
        )
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))
        object.__setattr__(
            self, "connections", MappingProxyType(dict(self.connections))
        )


def _index(label: str, specs: Iterable[Spec]) -> Mapping[str, Spec]:
    items = tuple(specs)
    indexed = {spec.name: spec for spec in items}
    if len(indexed) != len(items):
        raise ImproperlyConfigured(f"Duplicate {label} name in integration graph.")
    return indexed


def _validate_capability_resources(
    capabilities: Mapping[str, CapabilitySpec],
    resources: Mapping[str, ResourceSpec],
) -> None:
    missing = sorted(
        {
            spec.subject_resource
            for spec in capabilities.values()
            if spec.subject_resource and spec.subject_resource not in resources
        }
    )
    if missing:
        raise ImproperlyConfigured(
            f"Capabilities reference unknown resources: {', '.join(missing)}."
        )

    for capability in capabilities.values():
        if not capability.target_query or not capability.subject_resource:
            continue
        resource = resources[capability.subject_resource]
        if not resource.list_handler or not resource.list_query_type:
            raise ImproperlyConfigured(
                f"Capability {capability.name!r} cannot derive targets from "
                f"unlistable resource {resource.name!r}."
            )
        try:
            resource.list_query_type.model_validate(
                dict(capability.target_query), strict=True
            )
        except Exception as exc:
            raise ImproperlyConfigured(
                f"Capability {capability.name!r} has an invalid target query "
                f"for {resource.name!r}: {exc}"
            ) from exc


def _validate_connection_edges(
    connections: Mapping[str, ConnectionSpec],
    capabilities: Mapping[str, CapabilitySpec],
    resources: Mapping[str, ResourceSpec],
) -> None:
    unknown_capabilities = sorted(
        {
            ability.capability
            for connection in connections.values()
            for ability in connection.abilities
            if ability.capability and ability.capability not in capabilities
        }
    )
    if unknown_capabilities:
        raise ImproperlyConfigured(
            "Connection abilities reference unknown capabilities: "
            f"{', '.join(unknown_capabilities)}."
        )

    unknown_resources = sorted(
        {
            ability.subject_resource
            for connection in connections.values()
            for ability in connection.abilities
            if ability.subject_resource and ability.subject_resource not in resources
        }
    )
    if unknown_resources:
        raise ImproperlyConfigured(
            "Connection abilities reference unknown resources: "
            f"{', '.join(unknown_resources)}."
        )


def compile_integration_graph(
    *,
    capabilities: Iterable[CapabilitySpec],
    resources: Iterable[ResourceSpec],
    connections: Iterable[ConnectionSpec],
) -> IntegrationGraph:
    capability_index = _index("capability", capabilities)
    resource_index = _index("resource", resources)
    connection_index = _index("connection", connections)
    _validate_capability_resources(capability_index, resource_index)
    _validate_connection_edges(connection_index, capability_index, resource_index)
    return IntegrationGraph(capability_index, resource_index, connection_index)


def integration_graph() -> IntegrationGraph:
    return compile_integration_graph(
        capabilities=_collect_capabilities(),
        resources=_collect_resources(),
        connections=_collect_connections(),
    )
