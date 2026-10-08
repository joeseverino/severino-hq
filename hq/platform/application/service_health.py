"""One health rule for every service: the list's State, the page's Health, the API.

A declared service is as healthy as its parts. HQ's own name is up while it
answers the request asking, and needs attention when a finding names HQ's
machine, its service or a controller. A name nothing declares reads as what
the readings observing it say, and whether a DNS record names it.
"""

from dataclasses import dataclass
from typing import Any

from .projection import read_once
from .ui import counted

GOOD = "good"
ATTENTION = "attention"
SERIOUS = "serious"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
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

    faults = counted(len(service.faults), "setup problem", "setup problems") if service.faults else ""
    states = {facet.state for facet in service.facets}
    if SERIOUS in states:
        return Health(SERIOUS, "Has a problem", faults)
    if service.faults:
        return Health(ATTENTION, "Incomplete", faults)
    if ATTENTION in states:
        # Which parts, so the word comes with the thing to go and look at.
        waiting = [facet.label for facet in service.facets if facet.state == ATTENTION]
        return Health(ATTENTION, "Not checked yet", f"Not checked yet: {', '.join(waiting)}")
    return _in_place(service)


# What each part of a service is called in the sentence saying it was checked.
_PART_NOUNS = {"dns": "DNS record", "proxy": "proxy", "certificate": "certificate", "runtime": "container"}


def _in_place(service: Any) -> Health:
    """Every part HQ set up is in place: which parts those are, and what answers
    behind the proxy where HQ reads it.

    HQ checks records, never a request. So the word is what was checked, and a
    proxy forwarding to a port nothing publishes says that instead.
    """

    parts = [
        _PART_NOUNS.get(facet.id, facet.label.lower())
        for facet in service.facets
        if any(claim in service.declared_claims for claim in facet.claims)
    ]
    checked = f"{_and(parts)} {'is' if len(parts) == 1 else 'are'} in place" if parts else ""
    checked = checked[:1].upper() + checked[1:]
    if parts == [_PART_NOUNS["dns"]]:
        return Health(UNKNOWN, "DNS record only", "A DNS record is in place. Nothing HQ set up answers at it.")
    behind = _behind_the_proxy(service)
    if behind is None:
        return Health(GOOD, "Set up", f"{checked}." if checked else "")
    address, machine, container, reads_containers = behind
    if container:
        return Health(GOOD, "Set up", f"{checked} and forward to {container} on {machine}.")
    if reads_containers:
        return Health(
            ATTENTION,
            "Nothing on that port",
            f"{checked}. The proxy forwards to {address}, and no container on {machine} publishes that port.",
        )
    return Health(
        GOOD, "Set up", f"{checked}. The proxy forwards to {address}. HQ does not read what runs there."
    )


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def _behind_the_proxy(service: Any) -> tuple[str, str, str, bool] | None:
    """``(address, machine, container, whether HQ reads that machine's containers)``
    for where the proxy forwards, or None when no proxy forwards anywhere HQ can place."""

    from .whereabouts import reads_containers_on

    route = service.path.primary
    hops = list(route.hops) if route is not None else []
    upstream = next((index for index, hop in enumerate(hops) if hop.step == "upstream"), None)
    if upstream is None:
        return None
    after = hops[upstream + 1 :]
    machine = next((hop.name for hop in after if hop.step == "machine"), "")
    if not machine:
        return None
    container = next((hop.name for hop in after if hop.step == "container"), "")
    return hops[upstream].name, machine, container, reads_containers_on(machine)


def _undeclared(service: Any) -> Health:
    """What the readings observing the name say, and whether DNS names it."""

    path = service.path
    if not path.observed_line:
        return Health(UNKNOWN, "Nothing set up in HQ")
    said = [path.observed_line]
    if not path.routes and path.gaps:
        said.append(_dns_state(path.gaps[0]))
    return Health(UNKNOWN, "Read only", " · ".join(said))


def _dns_state(gap: str) -> str:
    return "DNS not read" if gap.startswith("not read:") else "No DNS record"


def _with_own_findings(found: Health) -> Health:
    """HQ's health, worsened to attention by each finding about HQ itself."""

    own = hq_findings()
    if not own:
        return found
    state = found.state if found.state == SERIOUS else ATTENTION
    about = counted(len(own), "open problem is about HQ", "open problems are about HQ")
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
