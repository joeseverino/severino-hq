"""Which devices can sign for tailnet lock.

Tailnet lock trusts a set of keys to sign a device onto the tailnet, and each
is the public half of a key one device holds. The lock reading names the keys
and the device reading names each device's key; the join engine joins the two
(``ObservationSpec.keys``, ``Machine.subject``), so a key no machine holds is
an unknown signer.
"""

from dataclasses import dataclass

from hq.domains.control_plane.observations.host import TAILNET_LOCK_KIND

from .facts import readings
from .projection import read_once
lazy from .connections import machines_once
lazy from .tailnet import policy

# What a key no machine HQ knows holds is called.
UNKNOWN_SIGNER = "Unknown signer"


@dataclass(frozen=True, slots=True)
class Signer:
    """One key tailnet lock trusts to sign, and the machine that holds it."""

    key: str
    votes: int = 0
    # The machine whose tailnet device holds the key; "" when none does.
    machine: str = ""
    # Whether that machine is one HQ runs on or manages (``Machine.is_server``).
    server: bool = False

    @property
    def label(self) -> str:
        return self.machine or UNKNOWN_SIGNER


def signers() -> tuple[Signer, ...]:
    """Every key the last lock reading trusts to sign, in its order; none while lock is off."""

    def build() -> tuple[Signer, ...]:
        lock = policy().lock
        if not lock.get("enabled"):
            return ()
        index = readings()
        held = {}
        for machine in machines_once():
            for joined in index.about(machine.subject, kinds=(TAILNET_LOCK_KIND,)):
                for key in joined.keys:
                    held.setdefault(key, machine)
        found = []
        for signer in lock.get("signers") or ():
            key = str(signer.get("key", ""))
            holder = held.get(key)
            found.append(
                Signer(
                    key=key,
                    votes=int(signer.get("votes") or 0),
                    machine=holder.name if holder is not None else "",
                    server=holder is not None and holder.is_server,
                )
            )
        return tuple(found)

    return read_once("tailnet.signers", build)


def signer_of(machine: str) -> Signer | None:
    """The signer ``machine``'s device is, when it is one."""

    return next((signer for signer in signers() if signer.machine == machine), None)
