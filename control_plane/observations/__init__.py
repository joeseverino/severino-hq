"""Readings: what a connection reports that HQ shows and never declares.

A resource kind is something HQ can create, reconcile or delete, and lives in
``providers.PROVIDERS``. A reading is only observed: a host's firewall, a Pages
project, an Access application. Each provider's readings live in their own
module and register here, with the schema of the only fields HQ keeps.

A reader that cannot read raises; the sweep stores the kind as unreachable with
the reason, and ``ObservationSpec.requires`` says what the credential needs.
"""

from __future__ import annotations

from . import adguard, cloudflare, github, host, hq, npm, portainer, public_registry, tailscale
from .contract import ObservationRecord, ObservationSpec, ReadingPart, registry

OBSERVATIONS = registry(
    host.OBSERVATIONS
    + cloudflare.OBSERVATIONS
    + tailscale.OBSERVATIONS
    + portainer.OBSERVATIONS
    + npm.OBSERVATIONS
    + public_registry.OBSERVATIONS
    + adguard.OBSERVATIONS
    + hq.OBSERVATIONS
    + github.OBSERVATIONS
)

__all__ = ["OBSERVATIONS", "ObservationRecord", "ObservationSpec", "ReadingPart", "registry"]
