"""Statically admitted controller provider adapters.

Each module here emits its whole contribution as ``ADAPTER``. Admission is this
one closed tuple, owned by HQ: a module outside it contributes nothing.
"""

from . import adguard, caddy, github, npm, portainer

ADMITTED = (npm, caddy, adguard, portainer, github)

CONTROLLER_PROVIDER_ADAPTERS = tuple(module.ADAPTER for module in ADMITTED)

__all__ = ["ADMITTED", "CONTROLLER_PROVIDER_ADAPTERS"]
