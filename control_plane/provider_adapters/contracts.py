"""Closed-world contract joining one provider's declaration and controller."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from types import MappingProxyType
from typing import Any, Protocol, TypeVar

# Connection providers the controller core probes itself, as a transport,
# rather than an integration declaring a probe for them.
CORE_PROBED_CONNECTIONS: frozenset[str] = frozenset({"ssh"})


T = TypeVar("T")


# Why a provider refused a read: the credential itself (invalid, expired,
# locked out, used from a refused location), or one permission it lacks.
CREDENTIAL_REFUSAL = "credential"
PERMISSION_REFUSAL = "permission"
REFUSALS = (CREDENTIAL_REFUSAL, PERMISSION_REFUSAL)

# Why a read got no API answer at all: the address answered as something other
# than the API (a sign-in page, a web page, a redirect elsewhere), or nothing
# answered (no route, refused connection, timeout).
ADDRESS_FAILURE = "address"
NETWORK_FAILURE = "network"
# Every cause a failed read is stored with: a refusal, or one of these.
FAILURES = (*REFUSALS, ADDRESS_FAILURE, NETWORK_FAILURE)

# Cloudflare's wording for a refusal of the credential itself rather than of
# one request. "Authentication error" (code 10000) alone is a missing permission.
_CLOUDFLARE_CREDENTIAL_REFUSALS = (
    "from location",
    "too many authentication failures",
    "invalid api token",
    "invalid access token",
    "expired",
)
_CLOUDFLARE_PERMISSION_REFUSAL = "authentication error"


def cloudflare_refusal(
    detail: str, *, status: int = 0, verified: Callable[[], bool] | None = None
) -> str:
    """Which of ``REFUSALS`` Cloudflare's error text and HTTP status name, or "".

    Cloudflare answers a missing permission with "Authentication error" under
    HTTP 403 on most endpoints and 401 on some. Under 401 the words alone cannot
    tell a missing permission from a dead credential, so ``verified`` (whether
    the credential itself still verifies) decides; without it, the credential is
    assumed refused.
    """

    lowered = str(detail or "").lower()
    if any(phrase in lowered for phrase in _CLOUDFLARE_CREDENTIAL_REFUSALS):
        return CREDENTIAL_REFUSAL
    if _CLOUDFLARE_PERMISSION_REFUSAL in lowered and status != 401:
        return PERMISSION_REFUSAL
    if status == 401:
        if _CLOUDFLARE_PERMISSION_REFUSAL in lowered and verified is not None and verified():
            return PERMISSION_REFUSAL
        return CREDENTIAL_REFUSAL
    if status == 403:
        return PERMISSION_REFUSAL
    return ""


@dataclass(frozen=True)
class IngressPolicy:
    """The source policy an observed proxy record applies to its names.

    ``rules`` are ``(directive, address)`` in order, lowercased; None when the
    record carries no rule-level reading. ``authorizations`` is None when unknown.
    """

    hostnames: tuple[str, ...]
    restricted: bool
    rules: tuple[tuple[str, str], ...] | None = None
    implicit_deny: bool = False
    satisfy_any: bool = True
    passes_auth: bool = True
    authorizations: int | None = None


@dataclass(frozen=True)
class ServedCertificate:
    """The certificate an observed record serves its names with.

    ``unread`` says why the record cannot name it; ``certificate`` is then empty.
    """

    hostnames: tuple[str, ...]
    certificate: Mapping[str, Any]
    unread: str = ""


class ProviderError(RuntimeError):
    """A provider operation failed without exposing credential material."""

    def __init__(
        self,
        message: str,
        *,
        status: dict[str, Any] | None = None,
        refusal: str = "",
        reason: str = "",
        failure: str = "",
    ):
        super().__init__(message)
        self.status = status or {}
        if refusal and refusal not in REFUSALS:
            raise ValueError(f"Unknown refusal {refusal!r}.")
        if failure and failure not in FAILURES:
            raise ValueError(f"Unknown failure {failure!r}.")
        # One of REFUSALS when the provider refused, and its words for why.
        self.refusal = refusal
        self.reason = reason
        # One of FAILURES: the refusal, or why nothing answered as the API.
        self.failure = refusal or failure


def failure_of(exc: BaseException) -> str:
    """Which of ``FAILURES`` an exception from a provider read names, or "".

    A ``ProviderError`` carries its own. An HTTP 401 refuses the credential and
    a 403 one permission; any other failure to connect is the network.
    """

    import urllib.error

    if isinstance(exc, ProviderError):
        return exc.failure
    if isinstance(exc, urllib.error.HTTPError):
        return {401: CREDENTIAL_REFUSAL, 403: PERMISSION_REFUSAL}.get(exc.code, "")
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return NETWORK_FAILURE
    return ""


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

    def connection_refs(self, provider: str) -> tuple[str, ...]:
        """Every connection of one provider, as its own item declares."""

        raise NotImplementedError

    def connection_refs_for_role(self, role: str) -> tuple[str, ...]:
        raise NotImplementedError

    def controller_id(self) -> str:
        """The machine the controller runs on, as the topology names it."""

        raise NotImplementedError

    def own_run(self, container: Mapping[str, Any]) -> bool:
        """Whether a container is the controller running this sweep."""

        raise NotImplementedError

    def ssh(
        self, connection_ref: str, operation: str, payload: bytes | None = None
    ) -> bytes:
        raise NotImplementedError

    def run(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        step: str = "command",
    ) -> bytes:
        """Run a local command line tool, for an integration that only has one.

        ``env`` is added to the controller's own environment for that one call
        and is the only way to pass a credential: an argument list is visible to
        every process on the machine, and the host scrubs these values out of
        anything it logs.
        """

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
    # Readers for registered readings (``control_plane.observations``) taken
    # through this integration's own connections.
    readings: Mapping[str, ProviderInventory] = field(default_factory=dict)
    # Connections the readings go through that no definition here declares:
    # an integration whose resources the controller core still holds.
    reads_through: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        definitions = tuple(self.definitions)
        inventory = MappingProxyType(dict(self.inventory))
        probes = MappingProxyType(dict(self.connection_probes))
        actions = MappingProxyType(dict(self.actions))
        readings = MappingProxyType(dict(self.readings))
        object.__setattr__(self, "definitions", definitions)
        object.__setattr__(self, "inventory", inventory)
        object.__setattr__(self, "connection_probes", probes)
        object.__setattr__(self, "actions", actions)
        object.__setattr__(self, "readings", readings)

        kinds = [definition.kind for definition in definitions]
        if not kinds and not readings:
            raise ValueError("Controller integration must define a resource kind or a reading.")
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
            if provider not in CORE_PROBED_CONNECTIONS
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
        object.__setattr__(self, "reads_through", tuple(self.reads_through))
        _admit_readings(readings, definitions, self.reads_through)
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


def _admit_readings(
    readings: Mapping[str, ProviderInventory],
    definitions: tuple[ControllerProviderDefinition, ...],
    reads_through: tuple[str, ...],
) -> None:
    """Each reading is registered and read through a connection this integration holds."""

    from ..observations import OBSERVATIONS

    providers = {
        provider for definition in definitions for provider in definition.connection_providers
    } | set(reads_through)
    for kind in readings:
        spec = OBSERVATIONS.get(kind)
        if spec is None:
            raise ValueError(f"Controller integration reads unregistered reading {kind!r}.")
        if spec.provider not in providers:
            raise ValueError(
                f"Controller integration reads {kind!r} through {spec.provider!r}, "
                "a connection it does not hold."
            )


@dataclass(frozen=True)
class ControllerAdapterRegistry:
    definitions: Mapping[str, ControllerProviderDefinition]
    inventory: Mapping[str, Callable[[], list[dict[str, Any]]]]
    connection_probes: Mapping[str, Callable[[str], dict[str, Any]]]
    actions: Mapping[tuple[str, str], Callable[..., ProviderResult]]
    readings: Mapping[str, Callable[[], list[dict[str, Any]]]] = field(
        default_factory=dict
    )


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
    readings: dict[str, Callable[[], list[dict[str, Any]]]] = {}
    for adapter in adapters:
        for kind, reader in adapter.inventory.items():
            inventory[kind] = partial(reader, runtime)
        for kind, reader in adapter.readings.items():
            if kind in readings:
                raise ValueError(f"Duplicate reader for {kind!r}.")
            readings[kind] = partial(reader, runtime)
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
        readings=MappingProxyType(readings),
    )
