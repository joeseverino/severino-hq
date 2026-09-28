"""The provider modules HQ admits, and every declaration they emit.

Each module declares its own kinds. One whose controller half lives here emits
its whole contribution as ``ADAPTER``; one whose actions are still the
controller core's emits its declarations as ``DEFINITIONS``. Admission is this
one closed tuple, owned by HQ, and its order is the registry's order: a module
outside it contributes nothing.
"""

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

CONTROLLER_PROVIDER_ADAPTERS = tuple(
    module.ADAPTER for module in ADMITTED if hasattr(module, "ADAPTER")
)

DECLARATIONS = tuple(
    definition
    for module in ADMITTED
    for definition in (
        *getattr(module, "DEFINITIONS", ()),
        *(module.ADAPTER.definitions if hasattr(module, "ADAPTER") else ()),
    )
)

__all__ = ["ADMITTED", "CONTROLLER_PROVIDER_ADAPTERS", "DECLARATIONS"]
