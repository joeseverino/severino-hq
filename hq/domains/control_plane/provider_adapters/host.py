"""HQ's own systemd units on the machine its controller runs on, as declared.

The declared set is the set the release installed: a sweep takes on each unit
the host.unit reading finds that the repository ships, and forgets one the
reading no longer lists, so nobody adopts or removes one by hand. The
controller acts on them through root's unit socket on its own machine
(controller/host/unit), which checks the name against the installed set
itself; nothing here starts or enables a unit unasked.
"""

from typing import Any

from pydantic import Field

from ..observations import host as readings
from ..observations.host import UnitName
from ..provider_spec import ProviderModel, ProviderSpec, applies, key_from, locked


class HostUnitSpec(ProviderModel):
    """One unit HQ's release installs, by name: identity and nothing else,
    since its unit file is the release's and HQ does not edit it."""

    unit: UnitName = Field(title="Unit", description="The unit's name, as systemd knows it.")


def _unit_from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {"unit": record.get("unit", "")}


def _unit_identity(spec: dict[str, Any]) -> tuple[str, ...]:
    return (spec.get("unit", ""),)


UNIT = ProviderSpec(
    readings.UNIT_KIND,
    "A background job HQ's release installs on HQ's machine, which HQ can start, restart, enable or clear.",
    HostUnitSpec,
    actions={
        # Nothing converges and nothing runs unasked: a deploy stops some of
        # these on purpose while it replaces the release.
        "reconcile": locked("Saved in HQ. Its unit file is the release's and does not change."),
        "start": applies(),
        "restart": applies(),
        "reset-failed": applies(),
        "enable": applies(),
    },
    label="Background job",
    readout=lambda spec, status: (("Unit", spec.get("unit", ""), status.get("unit", "")),),
    _from_record=_unit_from_record,
    # A service of the machine's own, read because systemd restarted it, is
    # not HQ's to act on.
    adopts=lambda record: not record.get("foreign"),
    listed_whole=lambda spec: "",
    sample_record={"unit": "example-job.service", "load": "loaded", "active": "active", "sub": "running"},
    identity=_unit_identity,
    key_hint=lambda spec: key_from(spec.get("unit", "")),
    removal_note=lambda spec: (
        f"HQ stops tracking {spec.get('unit', 'this unit')} until its next reading. The unit keeps running."
    ),
    declaration_only=True,
)

DEFINITIONS = (UNIT,)
# The readings module's declarations, which admitting this adapter admits.
OBSERVATIONS = readings.OBSERVATIONS
INBOUND = readings.INBOUND
SHARED = readings.SHARED
