"""Readings: what a connection reports that HQ shows and never declares.

A reading is only observed (a host's firewall, a Pages project), where a
resource kind in ``providers.PROVIDERS`` is something HQ can create, reconcile
or delete. Each provider's readings are a module here, registered by discovery;
the rules every reading keeps are docs/Derived-Facts.md#readings.
"""

from importlib import import_module
from pkgutil import iter_modules

from .contract import ObservationRecord, ObservationSpec, ReadingPart, Stamp, registry

# Every module beside this one is a provider's readings, and declares them as
# ``OBSERVATIONS``: adding one is adding its file. A spec is only a schema; what
# may read it is still admitted by ``provider_adapters.ADMITTED``. Modules are
# gathered in name order, so the registry's order does not depend on who
# imported what first.
_SPEC_MODULES = tuple(
    import_module(f"{__name__}.{info.name}")
    for info in sorted(iter_modules(__path__), key=lambda info: info.name)
    if info.name != "contract" and not info.name.startswith(("_", "test"))
)

OBSERVATIONS = registry(tuple(spec for module in _SPEC_MODULES for spec in module.OBSERVATIONS))

__all__ = [
    "OBSERVATIONS",
    "ObservationRecord",
    "ObservationSpec",
    "ReadingPart",
    "Stamp",
    "registry",
]
