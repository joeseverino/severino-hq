"""The provider modules HQ admits, and every declaration they emit.

Each module declares everything it brings: its kinds (``DEFINITIONS``) and the
connection its credential arrives through (``CONNECTIONS``). The controller
(controller/providers) reads and acts on them. Admission is this one closed
tuple, owned by HQ, and its order is the registries' order: a module outside it
contributes nothing, and adding a provider is writing its module and naming it
here.
"""

from ..provider_spec import ConnectionKind
from . import (
    adguard,
    caddy,
    cloudflare,
    declarations,
    github,
    npm,
    portainer,
    tailscale,
    tls,
)

ADMITTED = (tls, npm, github, portainer, tailscale, declarations, caddy, adguard, cloudflare)

DECLARATIONS = tuple(definition for module in ADMITTED for definition in module.DEFINITIONS)


def admitted_connections(modules) -> dict[str, ConnectionKind]:
    """Every connection provider, named once, in admission order."""

    found: dict[str, ConnectionKind] = {}
    for provider, kind in (item for module in modules for item in getattr(module, "CONNECTIONS", {}).items()):
        if provider in found:
            raise ValueError(f"Two modules declare the connection provider {provider!r}.")
        found[provider] = kind
    return found


def undeclared_connections(definitions, connections) -> list[str]:
    """Connection providers a kind names that no admitted module declares."""

    return sorted(
        {
            provider
            for definition in definitions
            for provider in definition.connection_providers
            if provider not in connections
        }
    )


CONNECTIONS = admitted_connections(ADMITTED)

if undeclared := undeclared_connections(DECLARATIONS, CONNECTIONS):
    raise ValueError(f"A kind names connection providers no admitted module declares: {undeclared}.")

__all__ = [
    "ADMITTED",
    "CONNECTIONS",
    "DECLARATIONS",
    "admitted_connections",
    "undeclared_connections",
]
