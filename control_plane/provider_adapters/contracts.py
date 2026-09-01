"""Closed-world contract joining one provider's declaration and controller."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Any, Protocol


class ProviderError(RuntimeError):
    """A provider operation failed without exposing credential material."""

    def __init__(self, message: str, *, status: dict[str, Any] | None = None):
        super().__init__(message)
        self.status = status or {}


@dataclass(frozen=True)
class ProviderResult:
    changed: bool
    status: dict[str, Any]
    conditions: list[dict[str, Any]]
    message: str


class ProviderRuntime(Protocol):
    """The deliberately small host surface an adapter may use."""

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any: ...

    def required(self, prefix: str, name: str) -> str: ...

    def connection_prefix(self, provider: str, connection_ref: str = "") -> str: ...

    def condition(
        self, condition_type: str, status: bool, reason: str, message: str
    ) -> dict[str, Any]: ...

    def ssh_connection_refs(self) -> tuple[str, ...]: ...

    def ssh(
        self, connection_ref: str, operation: str, payload: bytes | None = None
    ) -> bytes: ...


class ControllerActionDefinition(Protocol):
    mode: str


class ControllerProviderDefinition(Protocol):
    kind: str
    actions: Mapping[str, ControllerActionDefinition]
    connection_providers: tuple[str, ...]
    unobserved_reason: str


ProviderAction = Callable[..., ProviderResult]
ProviderInventory = Callable[[ProviderRuntime], list[dict[str, Any]]]
ConnectionProbe = Callable[[ProviderRuntime, str], dict[str, Any]]


@dataclass(frozen=True)
class ControllerProviderAdapter:
    """One statically admitted provider's complete controller contribution."""

    definition: ControllerProviderDefinition
    inventory: ProviderInventory | None
    connection_probes: Mapping[str, ConnectionProbe]
    actions: Mapping[str, ProviderAction]

    def __post_init__(self) -> None:
        probes = MappingProxyType(dict(self.connection_probes))
        actions = MappingProxyType(dict(self.actions))
        object.__setattr__(self, "connection_probes", probes)
        object.__setattr__(self, "actions", actions)

        kind = self.definition.kind
        declared_actions = {
            name
            for name, policy in self.definition.actions.items()
            if policy.mode == "apply"
        }
        if set(actions) != declared_actions:
            raise ValueError(
                f"Controller adapter {kind!r} actions do not match its declaration: "
                f"expected {sorted(declared_actions)}, got {sorted(actions)}."
            )

        declared_probes = {
            provider
            for provider in self.definition.connection_providers
            if provider != "ssh"
        }
        if set(probes) != declared_probes:
            raise ValueError(
                f"Controller adapter {kind!r} probes do not match its connections: "
                f"expected {sorted(declared_probes)}, got {sorted(probes)}."
            )

        if self.inventory is None and not self.definition.unobserved_reason:
            raise ValueError(
                f"Controller adapter {kind!r} has no inventory reader and says no reason."
            )
        if self.inventory is not None and self.definition.unobserved_reason:
            raise ValueError(
                f"Controller adapter {kind!r} both emits inventory and claims it is unobserved."
            )


@dataclass(frozen=True)
class ControllerAdapterRegistry:
    definitions: Mapping[str, ControllerProviderDefinition]
    inventory: Mapping[str, Callable[[], list[dict[str, Any]]]]
    connection_probes: Mapping[str, Callable[[str], dict[str, Any]]]
    actions: Mapping[tuple[str, str], Callable[..., ProviderResult]]


def admit_controller_adapters(
    adapters: tuple[ControllerProviderAdapter, ...],
) -> Mapping[str, ControllerProviderDefinition]:
    """Validate the closed adapter set before any consumer derives from it."""

    definitions: dict[str, ControllerProviderDefinition] = {}
    for adapter in adapters:
        kind = adapter.definition.kind
        if kind in definitions:
            raise ValueError(f"Duplicate controller adapter for {kind!r}.")
        definitions[kind] = adapter.definition
    return MappingProxyType(definitions)


def compile_controller_adapters(
    adapters: tuple[ControllerProviderAdapter, ...], runtime: ProviderRuntime
) -> ControllerAdapterRegistry:
    """Admit a static adapter set or fail before the controller can run."""

    definitions = admit_controller_adapters(adapters)
    inventory: dict[str, Callable[[], list[dict[str, Any]]]] = {}
    probes: dict[str, Callable[[str], dict[str, Any]]] = {}
    actions: dict[tuple[str, str], Callable[..., ProviderResult]] = {}
    for adapter in adapters:
        kind = adapter.definition.kind
        if adapter.inventory is not None:
            inventory[kind] = partial(adapter.inventory, runtime)
        for provider, probe in adapter.connection_probes.items():
            if provider in probes:
                raise ValueError(f"Duplicate connection probe for {provider!r}.")
            probes[provider] = partial(probe, runtime)
        for action, handler in adapter.actions.items():
            identity = (kind, action)
            if identity in actions:
                raise ValueError(f"Duplicate controller action for {kind!r}/{action!r}.")
            actions[identity] = partial(handler, runtime)
    return ControllerAdapterRegistry(
        definitions=definitions,
        inventory=MappingProxyType(inventory),
        connection_probes=MappingProxyType(probes),
        actions=MappingProxyType(actions),
    )
