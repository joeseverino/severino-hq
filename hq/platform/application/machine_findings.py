"""Findings about whether a machine the estate depends on is there at all.

A machine Tailscale has not seen for ``OFFLINE_AFTER`` that holds something
(a declaration, a name it serves, a container, a role) is offline. A person's
laptop asleep or phone away is normal, not an outage, so a personal device is
never raised. Read from the estate (``estate.estate_reading``) and raised under
Estate, about the machine's own node.
"""

from datetime import timedelta

from .derivations import reached
from .finding_model import Finding, FindingEstate, FindingRule, opens
from .moments import ago, duration, moment
from .tone import Tone
from .ui import counted, ended
lazy from .estate import estate_reading

OFFLINE = "machine-offline"
# A machine that holds something for the estate and has been offline this long
# is a finding. Shorter gaps are restarts.
OFFLINE_AFTER = timedelta(hours=1)


def _holds_something(machine) -> bool:
    return bool(machine.declaration or machine.hostnames or machine.containers or machine.roles or machine.runs_hq)


def _offline(_estate: FindingEstate) -> tuple[Finding, ...]:
    found = []
    for machine in estate_reading().machines:
        presence = machine.presence
        if presence is None or presence.online or presence.personal or not _holds_something(machine):
            continue
        seen = moment(presence.last_seen)
        if seen is None or seen.year < 2000 or not reached(seen + OFFLINE_AFTER):
            continue
        what = ", ".join(role.label for role in machine.roles)
        found.append(
            Finding(
                rule=OFFLINE,
                subject=f"machine:{machine.name}",
                title=f"{machine.name} is offline",
                severity=Tone.SERIOUS if machine.hostnames or machine.roles or machine.runs_hq else Tone.ATTENTION,
                explanation=(
                    f"Last seen {ago(seen)}."
                    + (ended(f" It is the {what}") if what else "")
                    + (f" It serves {counted(len(machine.hostnames), 'name')}." if machine.hostnames else "")
                ),
                evidence=(("Last seen by Tailscale", ago(seen)),),
                primary=opens("Open machine", machine.url),
            )
        )
    return tuple(found)


# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        OFFLINE,
        "Machines offline",
        Tone.ATTENTION,
        _offline,
        operator_action="Bring the machine back on the machine itself: power, network, then Tailscale.",
        no_help_reason="HQ has no power or console access to machines. Bring it back on the machine itself.",
        bars={
            Tone.SERIOUS: f"a machine that serves a name or a role has been away for {duration(OFFLINE_AFTER)}",
            Tone.ATTENTION: f"a machine that serves no name or role has been away for {duration(OFFLINE_AFTER)}",
        },
        clears="Tailscale sees the machine again",
        domain="hq.estate",
        gathered=True,
    ),
)
