"""One health rule for every service: the list's State, the page's Health, the API.

A declared service is as healthy as its parts. HQ's own name is up while it
answers the request asking, and needs attention when a finding names HQ's
machine, its service or a controller. A name nothing declares reads as what
the readings observing it say, and whether a DNS record names it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .projection import read_once
from .ui import counted

GOOD = "good"
ATTENTION = "attention"
SERIOUS = "serious"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Health:
    state: str
    label: str
    detail: str = ""

    @property
    def tone(self) -> str:
        """The state as a tone: "unknown" is no health reading, so neutral."""

        return self.state if self.state in (GOOD, ATTENTION, SERIOUS) else "neutral"


def service_health(service: Any, *, own_findings: bool = True) -> Health:
    """The health of one listed service.

    ``own_findings=False`` leaves HQ's findings out, for the topology the
    findings are derived from.
    """

    if service.declared_claims:
        found = _declared(service)
    elif service.is_hq:
        found = Health(GOOD, "Up", "Answering this request.")
    else:
        return _undeclared(service)
    return _with_own_findings(found) if service.is_hq and own_findings else found


def _declared(service: Any) -> Health:
    """Worst news first, with a live failure outranking a wiring gap.

    A declared part that was working and is not is serious; a name never fully
    wired, or whose parts no controller has confirmed yet, needs attention.
    """

    faults = counted(len(service.faults), "wiring fault", "wiring faults") if service.faults else ""
    states = {facet.state for facet in service.facets}
    if SERIOUS in states:
        return Health(SERIOUS, "Degraded", faults)
    if service.faults:
        return Health(ATTENTION, "Incomplete", faults)
    if ATTENTION in states:
        # Which parts, so the word comes with the thing to go and look at.
        waiting = [facet.label for facet in service.facets if facet.state == ATTENTION]
        return Health(ATTENTION, "Unverified", f"{', '.join(waiting)} not confirmed yet")
    # What "healthy" rests on, said: every declared part confirmed.
    return Health(GOOD, "Healthy", counted(len(service.declared_claims), "part confirmed", "parts confirmed"))


def _undeclared(service: Any) -> Health:
    """What the readings observing the name say, and whether DNS names it."""

    path = service.path
    if not path.observed_line:
        return Health(UNKNOWN, "Nothing declared")
    said = [path.observed_line]
    if not path.routes and path.gaps:
        said.append(_dns_state(path.gaps[0]))
    return Health(UNKNOWN, "Observed", " · ".join(said))


def _dns_state(gap: str) -> str:
    return "DNS not read" if gap.startswith("not read:") else "No DNS record"


def _with_own_findings(found: Health) -> Health:
    """HQ's health, worsened to attention by each finding about HQ itself."""

    own = hq_findings()
    if not own:
        return found
    state = found.state if found.state == SERIOUS else ATTENTION
    about = counted(len(own), "finding names HQ", "findings name HQ")
    return Health(state, found.label, " ".join(part for part in (found.detail, f"{about}.") if part))


def hq_findings() -> tuple[Any, ...]:
    """Findings whose subject is HQ's machine, one of its names, or a controller."""

    return read_once("service_health.hq_findings", _hq_findings)


def _hq_findings() -> tuple[Any, ...]:
    from .connections import machines_once
    from .findings import estate_findings
    from .hq_self import hq_service
    from .security import Capability, Principal

    own = hq_service(catalog=machines_once())
    if own is None:
        return ()
    subjects = {f"service:{name}" for name in own.hostnames}
    if own.machine:
        subjects.add(f"machine:{own.machine}")
    reader = Principal("hq-health", "internal", frozenset({Capability.READ}))
    return tuple(
        finding
        for finding in estate_findings(principal=reader)
        if finding.subject in subjects or finding.subject.startswith("controller:")
    )
