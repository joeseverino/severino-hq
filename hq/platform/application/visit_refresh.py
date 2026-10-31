"""Asking for a page's own readings when somebody opens it.

Whoever opened a page about one machine, service or record is looking at its
readings now, so it asks for what it is assembled from (docs/Design.md, "A page
about one thing asks"). It names only its subject, never a connection or a kind, so this
cannot become a way to make HQ call its providers. This decides when to ask and
for what; ``cadence.request_reads`` asks and ``asks.read_standing`` follows.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any

from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from hq.domains.control_plane.observations.github import REPOSITORY_KIND
lazy from hq.domains.control_plane.observations import OBSERVATIONS
lazy from hq.domains.control_plane.providers import PROVIDERS

from .asks import Standing, read_standing, read_status_url
from .cadence import controller_standing, forced_reads, request_reads, swept
from .command_fields import Describe
from .freshness import PAGE_VISIT, freshness
from .record_errors import RecordNotFound
from .security import Principal
lazy from . import machines
lazy from .credential_sight import fed_kinds
lazy from .entity_links import entity_link
lazy from .service_list import listed_service
lazy from .services import alias_target


@dataclass(frozen=True, slots=True)
class Reads:
    """The kinds one page is assembled from."""

    kinds: tuple[str, ...] = ()


def askable(kind: str) -> bool:
    """Whether opening a page may ask for ``kind``.

    The sweep must read it, or the request is never answered and forces sweeps
    until it expires. And its reader must not log in to a machine: a host
    counts its logins, so those are left to the sweep's own clock. A kind
    nothing declares is neither, so it is not asked for.
    """

    if not swept(kind):
        return False
    reading = OBSERVATIONS.get(kind)
    if reading is not None:
        return reading.provider != "ssh"
    return "ssh" not in PROVIDERS[kind].connection_providers


def _machine_reads(name: str) -> Reads | None:
    """A machine is read through the connections that reach it: what their
    credentials read is what its page is made of."""

    found = machines.machine(name)
    if found is None:
        return None
    providers = sorted(
        set(
            ProviderConnection.objects.filter(connection_ref__in=found.reached_by)
            .exclude(provider="")
            .values_list("provider", flat=True)
        )
    )
    return Reads(tuple(dict.fromkeys(kind for provider in providers for kind in fed_kinds(provider))))


def _service_reads(hostname: str) -> Reads | None:
    """A service is read through the kinds its declarations are."""

    if not entity_link("service", hostname).url:
        return None
    service = listed_service(alias_target(hostname) or hostname)
    return Reads(tuple(sorted({claim.kind for facet in service.facets for claim in facet.claims})))


def _resource_reads(key: str) -> Reads | None:
    """A record is read as its kind: a container's state and the verbs its
    page offers come from the sweep of containers."""

    kind = ManagedResource.objects.filter(key=key).values_list("kind", flat=True).first()
    return None if kind is None else Reads((kind,))


# A page about no one thing, by name, and what it is assembled from: the
# dashboard's queue says what GitHub holds, so opening it asks GitHub again.
PAGES: dict[str, Reads] = {"dashboard": Reads((REPOSITORY_KIND,))}

SUBJECTS: dict[str, Callable[[str], Reads | None]] = {
    "machine": _machine_reads,
    "service": _service_reads,
    "resource": _resource_reads,
    "page": PAGES.get,
}


def reads_of(subject: str, name: str) -> Reads | None:
    """What the page for ``subject`` ``name`` is assembled from; None when HQ
    has no such page."""

    lookup = SUBJECTS.get(subject)
    return lookup(name) if lookup and name else None


def _due(kinds: tuple[str, ...], now: datetime) -> list[str]:
    """The kinds old enough to ask for again, not already asked for."""

    asked = {read.kind for read in forced_reads() if read.kind}
    # Judged by the last attempt, not the last reading that worked: a provider
    # that is down or refusing stores nothing new, and judged by its last good
    # reading it would be due again the moment it was asked, without end.
    attempted = {
        kind: max(observed_at, updated_at)
        for kind, observed_at, updated_at in ProviderInventory.objects.filter(kind__in=kinds).values_list(
            "kind", "observed_at", "updated_at"
        )
    }
    return [
        kind
        # One no sweep has stored cannot be answered, so it is not asked for.
        for kind in kinds
        if kind in attempted and kind not in asked and askable(kind) and freshness(PAGE_VISIT, attempted[kind], now).due
    ]


def worth_asking(subject: str, name: str) -> bool:
    """Whether opening the page would ask for anything now. A page that
    renders its ask only then posts nothing on a visit that would read nothing."""

    reads = reads_of(subject, name)
    return bool(reads and _due(reads.kinds, timezone.now()))


def _watching(kinds: tuple[str, ...]) -> tuple[list[str], datetime | None]:
    """Of ``kinds``, the ones a read request is still waiting on, and when the
    earliest of those was asked for."""

    waiting: dict[str, datetime] = {}
    for read in forced_reads():
        for kind in (read.kind,) if read.kind else read.kinds or ():
            waiting[kind] = min(read.requested_at, waiting.get(kind, read.requested_at))
    found = [kind for kind in kinds if kind in waiting]
    return found, min((waiting[kind] for kind in found), default=None)


def _answer(requested: list[str], kinds: tuple[str, ...]) -> dict[str, Any]:
    """How the page's readings stand, and where to follow them while one is
    being read: the shape every ask answers in."""

    watching, asked = _watching(kinds) if kinds else ([], None)
    if asked is None:
        return {**Standing().as_json(), "requested": requested, "status": ""}
    return {
        **read_standing(watching, asked).as_json(),
        "requested": requested,
        "status": read_status_url(watching, asked),
    }


VISIT_REFRESH = "visit.refresh"


@dataclass(frozen=True, slots=True)
class VisitRefreshCommand:
    """The page that was opened: what it is about, and which one."""

    subject: Annotated[str, Describe("What type of page was opened.")]
    name: Annotated[str, Describe("Which one.")]


def request_visit_refresh(command: VisitRefreshCommand, *, principal: Principal) -> dict[str, Any]:
    """Ask for the readings behind one page that are due.

    Asks for nothing, and promises nothing, while no controller is arriving to
    read it: a request then would force sweeps when it returns, for a page
    that has long been closed.
    """

    del principal
    reads = reads_of(command.subject, command.name)
    if reads is None:
        raise RecordNotFound("HQ has no such page.")
    standing = controller_standing()
    if not standing.known or standing.silent:
        return {"ok": True, **_answer([], ())}
    requested = list(request_reads(_due(reads.kinds, timezone.now())))
    return {"ok": True, **_answer(requested, reads.kinds)}
