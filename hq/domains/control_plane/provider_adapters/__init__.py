"""The modules HQ admits, and every declaration and reading they emit.

Admission is this closed tuple, owned by HQ, and its order is the registries'
order: a module outside it contributes nothing. What each module declares, and
what admitting one takes, is docs/Architecture.md#providers.
"""

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from ..observations import glance, host, hq, public_registry
from ..observations.contract import ObservationSpec, registry
from ..provider_spec import ConnectionKind, InboundSpec
from . import (
    adguard,
    automation,
    caddy,
    cloudflare,
    declarations,
    github,
    npm,
    portainer,
    r2,
    tailscale,
    tls,
)

# A provider is its adapter, which brings its readings module's OBSERVATIONS
# with it. A source HQ reads with no connection a person makes (the
# controller's own machine, HQ's own requests, the keyless public registries)
# has no adapter, so its readings module is admitted alone.
ADMITTED = (
    tls,
    npm,
    github,
    portainer,
    tailscale,
    declarations,
    caddy,
    adguard,
    cloudflare,
    r2,
    automation,
    host,
    glance,
    hq,
    public_registry,
)

DECLARATIONS = tuple(definition for module in ADMITTED for definition in getattr(module, "DEFINITIONS", ()))

OBSERVATIONS: Mapping[str, ObservationSpec] = registry(
    tuple(spec for module in ADMITTED for spec in getattr(module, "OBSERVATIONS", ()))
)


def admitted(modules: Iterable[object], declaration: str) -> dict[str, Any]:
    """What the modules declare under ``declaration``, by provider, each
    named once, in admission order: their connections (``CONNECTIONS``) or
    the events they tell HQ of (``INBOUND``)."""

    found: dict[str, Any] = {}
    for provider, declared in (item for module in modules for item in getattr(module, declaration, {}).items()):
        if provider in found:
            raise ValueError(f"Two modules declare the {declaration} of {provider!r}.")
        found[provider] = declared
    return found


def undeclared_connections(definitions: Iterable[Any], connections: Collection[str]) -> list[str]:
    """Connection providers a kind names that no admitted module declares."""

    return sorted(
        {
            provider
            for definition in definitions
            for provider in definition.connection_providers
            if provider not in connections
        }
    )


CONNECTIONS: dict[str, ConnectionKind] = admitted(ADMITTED, "CONNECTIONS")
INBOUND: dict[str, InboundSpec] = admitted(ADMITTED, "INBOUND")

if undeclared := undeclared_connections(DECLARATIONS, CONNECTIONS):
    raise ValueError(f"A kind names connection providers no admitted module declares: {undeclared}.")

__all__ = [
    "ADMITTED",
    "CONNECTIONS",
    "DECLARATIONS",
    "INBOUND",
    "OBSERVATIONS",
    "admitted",
    "undeclared_connections",
]
