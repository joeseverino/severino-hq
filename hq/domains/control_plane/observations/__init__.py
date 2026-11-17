"""Readings: what a connection reports that HQ shows and never declares.

A reading is only observed (a host's firewall, a Pages project), where a
resource kind in ``providers.PROVIDERS`` is something HQ can create, reconcile
or delete. Each source's readings are a module here, and only a module
``provider_adapters.ADMITTED`` reaches is registered; the rules every reading
keeps are docs/Derived-Facts.md#readings.
"""

from .contract import ObservationRecord, ObservationSpec, ReadingPart, Stamp, registry

# The registry is gathered from the admitted modules, which import the readings
# modules here, so it is read from there only when first used.
lazy from ..provider_adapters import OBSERVATIONS

__all__ = [
    "OBSERVATIONS",
    "ObservationRecord",
    "ObservationSpec",
    "ReadingPart",
    "Stamp",
    "registry",
]
