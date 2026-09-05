"""Compile HQ's independently emitted contracts into one immutable graph."""

from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Any, Callable, Iterable, Iterator, Mapping, TypeVar

from django.core.exceptions import ImproperlyConfigured
from pydantic import ValidationError

from .connection_contracts import ConnectionSpec
from .contracts import DOTTED_NAME
from .integration_specs import CapabilitySpec, ResourceSpec
from .integration_validation import (
    validate_capability_spec,
    validate_connection_spec,
    validate_resource_spec,
)
from .search_contracts import SearchDefinition


Spec = TypeVar("Spec")


@dataclass(frozen=True, slots=True)
class IntegrationViolation:
    code: str
    message: str
    subjects: tuple[str, ...] = ()


class IntegrationGraphError(ImproperlyConfigured):
    """Every violation found while compiling one composition."""

    def __init__(self, violations: Iterable[IntegrationViolation]):
        self.violations = tuple(violations)
        rendered = "\n".join(
            f"- [{violation.code}] {violation.message}"
            for violation in self.violations
        )
        super().__init__(f"Invalid integration graph:\n{rendered}")


@dataclass(frozen=True, slots=True)
class IntegrationGraph:
    capabilities: Mapping[str, CapabilitySpec]
    resources: Mapping[str, ResourceSpec]
    connections: Mapping[str, ConnectionSpec]
    search: Mapping[str, SearchDefinition]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "capabilities", MappingProxyType(dict(self.capabilities))
        )
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))
        object.__setattr__(
            self, "connections", MappingProxyType(dict(self.connections))
        )
        object.__setattr__(self, "search", MappingProxyType(dict(self.search)))


def _index(
    label: str,
    specs: Iterable[Spec],
    violations: list[IntegrationViolation],
) -> Mapping[str, Spec]:
    items = tuple(specs)
    indexed = {spec.name: spec for spec in items}
    duplicates = tuple(
        sorted(name for name, count in Counter(spec.name for spec in items).items() if count > 1)
    )
    if duplicates:
        violations.append(
            IntegrationViolation(
                f"duplicate.{label}",
                f"Duplicate {label} names: {', '.join(duplicates)}.",
                duplicates,
            )
        )
    return indexed


def _validated_index(
    label: str,
    specs: Iterable[Any],
    expected_type: type[Spec],
    validate: Callable[[Spec], None],
    violations: list[IntegrationViolation],
) -> Mapping[str, Spec]:
    valid: list[Spec] = []
    for position, candidate in enumerate(specs):
        if not isinstance(candidate, expected_type):
            violations.append(
                IntegrationViolation(
                    f"invalid.{label}",
                    f"{label.title()} contribution {position} returned "
                    f"{type(candidate).__name__}, expected {expected_type.__name__}.",
                    (f"{label} contribution {position}",),
                )
            )
            continue
        try:
            validate(candidate)
        except ImproperlyConfigured as exc:
            violations.append(
                IntegrationViolation(
                    f"invalid.{label}", str(exc), (candidate.name,)
                )
            )
            continue
        valid.append(candidate)
    return _index(label, valid, violations)


def _validate_capability_resources(
    capabilities: Mapping[str, CapabilitySpec],
    resources: Mapping[str, ResourceSpec],
    violations: list[IntegrationViolation],
) -> None:
    missing = sorted(
        {
            spec.subject_resource
            for spec in capabilities.values()
            if spec.subject_resource and spec.subject_resource not in resources
        }
    )
    if missing:
        violations.append(
            IntegrationViolation(
                "capability.unknown_resource",
                f"Capabilities reference unknown resources: {', '.join(missing)}.",
                tuple(missing),
            )
        )

    for capability in capabilities.values():
        if not capability.target_query or not capability.subject_resource:
            continue
        if capability.subject_resource not in resources:
            continue
        resource = resources[capability.subject_resource]
        if not resource.list_handler or not resource.list_query_type:
            violations.append(
                IntegrationViolation(
                    "capability.unlistable_target_resource",
                    f"Capability {capability.name!r} cannot derive targets from "
                    f"unlistable resource {resource.name!r}.",
                    (capability.name, resource.name),
                )
            )
            continue
        try:
            resource.list_query_type.model_validate(
                dict(capability.target_query), strict=True
            )
        except ValidationError as exc:
            violations.append(
                IntegrationViolation(
                    "capability.invalid_target_query",
                    f"Capability {capability.name!r} has an invalid target query "
                    f"for {resource.name!r}: {exc}",
                    (capability.name, resource.name),
                )
            )


def _validate_connection_edges(
    connections: Mapping[str, ConnectionSpec],
    capabilities: Mapping[str, CapabilitySpec],
    resources: Mapping[str, ResourceSpec],
    violations: list[IntegrationViolation],
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
        violations.append(
            IntegrationViolation(
                "connection.unknown_capability",
                "Connection abilities reference unknown capabilities: "
                f"{', '.join(unknown_capabilities)}.",
                tuple(unknown_capabilities),
            )
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
        violations.append(
            IntegrationViolation(
                "connection.unknown_resource",
                "Connection abilities reference unknown resources: "
                f"{', '.join(unknown_resources)}.",
                tuple(unknown_resources),
            )
        )


def _index_search(
    resources: Mapping[str, ResourceSpec],
    standalone: Iterable[Any],
    violations: list[IntegrationViolation],
) -> Mapping[str, SearchDefinition]:
    candidates = (
        *(
            (f"resource {spec.name!r}", spec.search)
            for spec in resources.values()
            if spec.search is not None
        ),
        *(
            (f"standalone contribution {index}", definition)
            for index, definition in enumerate(standalone)
        ),
    )
    invalid = tuple(
        f"{source} returned {type(definition).__name__}"
        for source, definition in candidates
        if not isinstance(definition, SearchDefinition)
    )
    if invalid:
        violations.append(
            IntegrationViolation(
                "search.invalid_definition",
                "Search contributions must be SearchDefinition instances: "
                f"{', '.join(invalid)}.",
                invalid,
            )
        )
    definitions = tuple(
        definition
        for _, definition in candidates
        if isinstance(definition, SearchDefinition)
    )
    invalid_scopes = tuple(
        sorted(
            {
                definition.scope
                for definition in definitions
                if not DOTTED_NAME.fullmatch(definition.scope)
            }
        )
    )
    if invalid_scopes:
        violations.append(
            IntegrationViolation(
                "search.invalid_scope",
                f"Invalid search scopes: {', '.join(invalid_scopes)}.",
                invalid_scopes,
            )
        )
    duplicates = tuple(
        sorted(
            scope
            for scope, count in Counter(
                definition.scope for definition in definitions
            ).items()
            if count > 1
        )
    )
    if duplicates:
        violations.append(
            IntegrationViolation(
                "duplicate.search",
                f"Duplicate search scopes: {', '.join(duplicates)}.",
                duplicates,
            )
        )
    return {definition.scope: definition for definition in definitions}


def compile_integration_graph(
    *,
    capabilities: Iterable[CapabilitySpec],
    resources: Iterable[ResourceSpec],
    connections: Iterable[ConnectionSpec],
    search: Iterable[SearchDefinition] = (),
) -> IntegrationGraph:
    violations: list[IntegrationViolation] = []
    capability_index = _validated_index(
        "capability",
        capabilities,
        CapabilitySpec,
        validate_capability_spec,
        violations,
    )
    resource_index = _validated_index(
        "resource", resources, ResourceSpec, validate_resource_spec, violations
    )
    connection_index = _validated_index(
        "connection",
        connections,
        ConnectionSpec,
        validate_connection_spec,
        violations,
    )
    search_index = _index_search(resource_index, search, violations)
    _validate_capability_resources(capability_index, resource_index, violations)
    _validate_connection_edges(
        connection_index, capability_index, resource_index, violations
    )
    if violations:
        raise IntegrationGraphError(violations)
    return IntegrationGraph(
        capability_index, resource_index, connection_index, search_index
    )


@cache
def _compiled_integration_graph() -> IntegrationGraph:
    from .capabilities import CORE_CAPABILITY_SPECS
    from .domains import host_connection_specs
    from .plugins import (
        plugin_capability_specs,
        plugin_connection_specs,
        plugin_resource_specs,
        plugin_search_definitions,
    )
    from .resources import CORE_RESOURCE_SPECS

    return compile_integration_graph(
        capabilities=(*CORE_CAPABILITY_SPECS, *plugin_capability_specs()),
        resources=(
            *CORE_RESOURCE_SPECS,
            *plugin_resource_specs(),
        ),
        connections=(*host_connection_specs(), *plugin_connection_specs()),
        search=plugin_search_definitions(),
    )


_GRAPH_OVERRIDE: ContextVar[IntegrationGraph | None] = ContextVar(
    "integration_graph_override", default=None
)


def integration_graph() -> IntegrationGraph:
    return _GRAPH_OVERRIDE.get() or _compiled_integration_graph()


def clear_integration_graph_cache() -> None:
    _compiled_integration_graph.cache_clear()


@contextmanager
def override_integration_graph(graph: IntegrationGraph) -> Iterator[None]:
    """Install an explicit graph for one test or isolated composition proof."""

    token = _GRAPH_OVERRIDE.set(graph)
    try:
        yield
    finally:
        _GRAPH_OVERRIDE.reset(token)
