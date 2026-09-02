"""Closed-world contract joining one provider's declaration and controller."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from types import MappingProxyType
from typing import Any, Protocol, TypeVar


T = TypeVar("T")


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
    ) -> Any:
        raise NotImplementedError

    def required(self, prefix: str, name: str) -> str:
        raise NotImplementedError

    def connection_prefix(self, provider: str, connection_ref: str = "") -> str:
        raise NotImplementedError

    def snapshot_value(self, key: tuple[str, ...], load: Callable[[], T]) -> T:
        raise NotImplementedError

    def condition(
        self, condition_type: str, status: bool, reason: str, message: str
    ) -> dict[str, Any]:
        raise NotImplementedError

    def ssh_connection_refs(self) -> tuple[str, ...]:
        raise NotImplementedError

    def ssh(
        self, connection_ref: str, operation: str, payload: bytes | None = None
    ) -> bytes:
        raise NotImplementedError


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
class ControllerIntegrationAdapter:
    """One integration's complete, statically admitted controller contribution."""

    definitions: tuple[ControllerProviderDefinition, ...]
    inventory: Mapping[str, ProviderInventory]
    connection_probes: Mapping[str, ConnectionProbe]
    actions: Mapping[tuple[str, str], ProviderAction]

    def __post_init__(self) -> None:
        definitions = tuple(self.definitions)
        inventory = MappingProxyType(dict(self.inventory))
        probes = MappingProxyType(dict(self.connection_probes))
        actions = MappingProxyType(dict(self.actions))
        object.__setattr__(self, "definitions", definitions)
        object.__setattr__(self, "inventory", inventory)
        object.__setattr__(self, "connection_probes", probes)
        object.__setattr__(self, "actions", actions)

        kinds = [definition.kind for definition in definitions]
        if not kinds:
            raise ValueError("Controller integration must define a resource kind.")
        if len(kinds) != len(set(kinds)):
            raise ValueError("Controller integration contains duplicate definitions.")
        declared_actions = {
            (definition.kind, name)
            for definition in definitions
            for name, policy in definition.actions.items()
            if policy.mode == "apply"
        }
        if set(actions) != declared_actions:
            raise ValueError(
                "Controller integration actions do not match its declarations: "
                f"expected {sorted(declared_actions)}, got {sorted(actions)}."
            )

        declared_probes = {
            provider
            for definition in definitions
            for provider in definition.connection_providers
            if provider != "ssh"
        }
        if set(probes) != declared_probes:
            raise ValueError(
                "Controller integration probes do not match its connections: "
                f"expected {sorted(declared_probes)}, got {sorted(probes)}."
            )

        unknown_inventory = set(inventory) - set(kinds)
        if unknown_inventory:
            raise ValueError(
                "Controller integration inventories unknown kinds: "
                f"{sorted(unknown_inventory)}."
            )
        for definition in definitions:
            observed = definition.kind in inventory
            if not observed and not definition.unobserved_reason:
                raise ValueError(
                    f"Controller integration resource {definition.kind!r} has no inventory "
                    "reader and says no reason."
                )
            if observed and definition.unobserved_reason:
                raise ValueError(
                    f"Controller integration resource {definition.kind!r} both emits inventory "
                    "and claims it is unobserved."
                )


@dataclass(frozen=True)
class ControllerAdapterRegistry:
    definitions: Mapping[str, ControllerProviderDefinition]
    inventory: Mapping[str, Callable[[], list[dict[str, Any]]]]
    connection_probes: Mapping[str, Callable[[str], dict[str, Any]]]
    actions: Mapping[tuple[str, str], Callable[..., ProviderResult]]


def admit_controller_adapters(
    adapters: tuple[ControllerIntegrationAdapter, ...],
) -> Mapping[str, ControllerProviderDefinition]:
    """Validate the closed adapter set before any consumer derives from it."""

    definitions: dict[str, ControllerProviderDefinition] = {}
    for adapter in adapters:
        for definition in adapter.definitions:
            kind = definition.kind
            if kind in definitions:
                raise ValueError(f"Duplicate controller adapter for {kind!r}.")
            definitions[kind] = definition
    return MappingProxyType(definitions)


def compile_controller_adapters(
    adapters: tuple[ControllerIntegrationAdapter, ...], runtime: ProviderRuntime
) -> ControllerAdapterRegistry:
    """Admit a static adapter set or fail before the controller can run."""

    definitions = admit_controller_adapters(adapters)
    inventory: dict[str, Callable[[], list[dict[str, Any]]]] = {}
    probes: dict[str, Callable[[str], dict[str, Any]]] = {}
    actions: dict[tuple[str, str], Callable[..., ProviderResult]] = {}
    for adapter in adapters:
        for kind, reader in adapter.inventory.items():
            inventory[kind] = partial(reader, runtime)
        for provider, probe in adapter.connection_probes.items():
            if provider in probes:
                raise ValueError(f"Duplicate connection probe for {provider!r}.")
            probes[provider] = partial(probe, runtime)
        for identity, handler in adapter.actions.items():
            if identity in actions:
                raise ValueError(
                    f"Duplicate controller action for {identity[0]!r}/{identity[1]!r}."
                )
            actions[identity] = partial(handler, runtime)
    return ControllerAdapterRegistry(
        definitions=definitions,
        inventory=MappingProxyType(inventory),
        connection_probes=MappingProxyType(probes),
        actions=MappingProxyType(actions),
    )
