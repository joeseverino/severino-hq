"""One health rule for every service: the list's State, the page's Health, the API.

A declared service is as healthy as its parts. HQ's own name is up while it
answers the request asking, and stands as the ``hq`` record does when a
problem about HQ needs acting on (``hq_context.hq_problems``). A name nothing declares reads as what
the readings observing it say, and whether a DNS record names it.
"""

from dataclasses import dataclass
from typing import Any

from django.utils.text import capfirst, get_text_list

from hq.platform.application.labels import dotted

from .projection import read_once
from .tone import Tone
from .ui import agreed, counted, ended
lazy from . import hq_context
lazy from .moments import when
lazy from .security import INTERNAL_INTERFACE, Capability, Principal
lazy from .whereabouts import reads_containers_on


@dataclass(frozen=True, slots=True)
class Health:
    # None when nothing reads its health.
    state: Tone | None
    label: str
    detail: str = ""
    # Each part of a label that counts several kinds, in its own tone, so a
    # row says them as separate states that wrap between them.
    counts: tuple[tuple[Tone, str], ...] = ()

    @property
    def tone(self) -> Tone:
        """The state as a tone: no health reading is neutral."""

        return self.state or Tone.NEUTRAL

    @property
    def parts(self) -> tuple[tuple[Tone, str], ...]:
        """The states a row draws: each count, or the label alone."""

        return self.counts or ((self.tone, self.label),)


def service_health(service: Any, *, own_findings: bool = True) -> Health:
    """The health of one listed service.

    ``own_findings=False`` leaves HQ's findings out, for the topology the
    findings are derived from.
    """

    if service.declared_claims:
        found = _as_last_read(_declared(service), service)
    elif service.is_hq:
        found = Health(Tone.GOOD, "Up", "Answering this request.")
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
    if Tone.SERIOUS in states:
        return Health(Tone.SERIOUS, "Has a problem", faults)
    if service.faults:
        return Health(Tone.ATTENTION, "Incomplete", faults)
    if Tone.ATTENTION in states:
        # Which parts, so the word comes with the thing to go and look at.
        waiting = [facet.label for facet in service.facets if facet.state == Tone.ATTENTION]
        return Health(Tone.ATTENTION, "Not checked yet", f"Not checked yet: {', '.join(waiting)}")
    return _in_place(service)


# What each part of a service is called in the sentence saying it was checked.
_PART_NOUNS = {"dns": "DNS record", "proxy": "proxy", "certificate": "certificate", "runtime": "container"}


def _in_place(service: Any) -> Health:
    """Every part HQ set up is in place: which parts those are, and what answers
    behind the proxy where HQ reads it.

    HQ checks records, never a request. So the word is what was checked, and
    green only for something read running: records in place are configured, not
    healthy. A proxy forwarding to a port nothing publishes says that instead.
    """

    parts = [
        _PART_NOUNS.get(facet.id, facet.label.lower())
        for facet in service.facets
        if any(claim in service.declared_claims for claim in facet.claims)
    ]
    checked = f"{get_text_list(parts, 'and')} {agreed(len(parts), 'is', 'are')} in place" if parts else ""
    checked = capfirst(checked)
    if parts == [_PART_NOUNS["dns"]]:
        return Health(None, "DNS record only", "A DNS record is in place. Nothing HQ set up answers at it.")
    route = service.path.primary
    end = route.forwards[-1] if route is not None and route.forwards else None
    if end is None or not end.machine:
        return Health(None, "In place", ended(checked))
    if end.container:
        return _running(service.container, ended(f"{checked} and forward to {end.container} on {end.machine}"))
    if reads_containers_on(end.machine):
        return Health(
            Tone.ATTENTION,
            "Nothing on that port",
            f"{ended(checked)} The proxy forwards to {end.address}, and no container on {end.machine} publishes that port.",
        )
    return Health(
        None,
        "In place",
        f"{ended(checked)} The proxy forwards to {ended(end.address)} HQ does not read what runs there.",
    )


def _running(running: Any, detail: str) -> Health:
    """What the container behind the proxy was last read doing: its own health
    check where it has one, else whether it runs."""

    check = running.check if running is not None else None
    if check is not None:
        return Health(check.tone, capfirst(check.said), detail)
    if running is not None and running.state.running:
        return Health(Tone.GOOD, "Running", detail)
    return Health(None, "In place", detail)


def _as_last_read(found: Health, service: Any) -> Health:
    """A health whose route was read from an out-of-date reading
    (``freshness``) is what it was when last read, never a live state."""

    route = service.path.primary
    stale = route.stale_reading if route is not None else None
    if stale is None or found.state is None:
        return found
    said = f"{found.label} when last read, {when(stale.observed_at)}."
    return Health(
        Tone.SERIOUS if found.state == Tone.SERIOUS else Tone.ATTENTION,
        "Out of date",
        " ".join(part for part in (said, found.detail) if part),
    )


def _undeclared(service: Any) -> Health:
    """What the readings observing the name say, and whether DNS names it."""

    path = service.path
    if not path.observed_line:
        return Health(None, "Nothing set up in HQ")
    said = [path.observed_line]
    if not path.routes and path.gaps:
        said.append(_dns_state(path.gaps[0]))
    return Health(None, "Read only", dotted(*said))


def _dns_state(gap: str) -> str:
    return "DNS not read" if gap.startswith("not read:") else "No DNS record"


def _with_own_findings(found: Health) -> Health:
    """HQ's health, worsened by the problems about HQ itself, in the words the
    ``hq`` record says them.

    A word that is not already a problem gives way to that count: "Health
    check passing" drawn in the attention tone would contradict itself.
    """

    problems = read_once("service_health.hq_problems", _hq_problems)
    tone, said = hq_context.status(problems)
    if not tone.acted_on:
        return found
    if found.state in (Tone.ATTENTION, Tone.SERIOUS):
        return Health(Tone.worst((found.state, tone)), found.label, " ".join(filter(None, (found.detail, ended(said)))))
    return Health(tone, said, found.detail, counts=hq_context.counts(problems))


def _hq_problems() -> tuple[Any, ...]:
    reader = Principal("hq-health", INTERNAL_INTERFACE, frozenset({Capability.READ}))
    return hq_context.hq_problems(reader)
