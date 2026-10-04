"""Asking for a page's own readings when somebody opens it.

A sweep reads everything on a clock. A page about one machine or one service
is about a few readings, and whoever opened it is looking at those now: so the
open page asks, and only for what that page is assembled from.

The rules that keep that from becoming a way to make HQ call its providers:

The page names its subject and nothing else. Which kinds sit behind a machine
or a service is looked up here; a request cannot name a connection or a kind,
and a subject HQ does not know asks for nothing.

Opening a page never logs in to a machine. A kind whose reader opens a shell
is not asked for from here at all: the sweep reads those on its own clock, and
a host counts its logins.

A kind is asked for again only when its last *attempt* is older than
``freshness`` allows, here as everywhere. The attempt, not the last reading
that worked: a provider that is down or refusing stores nothing new, and
judged by its last good reading it would be due again the moment it had been
asked, and asked without end.

And nothing is asked for that could not be answered. A request nothing answers
forces sweeps until it expires, so only a kind the sweep reads and has stored,
while a controller is arriving to read it, is ever asked for.

Asking is ``cadence.request_reads``, which is how a read is requested. This
decides when to ask and for what, and nothing about how.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.core import signing
from django.utils import timezone

from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
from hq.platform.core.audit import operation_context

from .cadence import controller_standing, forced_reads, request_reads
from .freshness import PAGE_VISIT, freshness
from .security import Capability, Principal

# What a page watches for, handed back signed so the page can ask whether it
# has arrived without HQ working the page out again, and without the page
# being able to ask about anything else.
_WATCH_SALT = "hq.platform.application.visit_refresh.watch"
# Longer than a page waits for its reading.
_WATCH_SECONDS = 15 * 60


@dataclass(frozen=True)
class Reads:
    """The kinds one page is assembled from."""

    kinds: tuple[str, ...] = ()


def askable(kind: str) -> bool:
    """Whether opening a page may ask for ``kind``.

    The sweep must read it, or the request is never answered. And its reader
    must not log in to a machine: those are left to the sweep's own clock.
    A kind nothing declares is neither, so it is not asked for.
    """

    from hq.domains.control_plane.observations import OBSERVATIONS
    from hq.domains.control_plane.providers import PROVIDERS

    from .cadence import swept

    if not swept(kind):
        return False
    reading = OBSERVATIONS.get(kind)
    if reading is not None:
        return reading.provider != "ssh"
    return "ssh" not in PROVIDERS[kind].connection_providers


def _machine_reads(name: str) -> Reads | None:
    """A machine is read through the connections that reach it: what their
    credentials read is what its page is made of."""

    from .credential_sight import fed_kinds
    from .machines import machine

    found = machine(name)
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

    from .entity_links import entity_link
    from .service_list import listed_service
    from .services import alias_target

    if not entity_link("service", hostname).url:
        return None
    service = listed_service(alias_target(hostname) or hostname)
    return Reads(tuple(sorted({claim.kind for facet in service.facets for claim in facet.claims})))


SUBJECTS: dict[str, Callable[[str], Reads | None]] = {
    "machine": _machine_reads,
    "service": _service_reads,
}


def reads_of(subject: str, name: str) -> Reads | None:
    """What the page for ``subject`` ``name`` is assembled from; None when HQ
    has no such page."""

    lookup = SUBJECTS.get(subject)
    return lookup(name) if lookup and name else None


def _due(kinds: tuple[str, ...], now: datetime) -> list[str]:
    """The kinds old enough to ask for again, not already asked for."""

    asked = {read.kind for read in forced_reads() if read.kind}
    attempted = {
        kind: max(observed_at, updated_at)
        for kind, observed_at, updated_at in ProviderInventory.objects.filter(
            kind__in=kinds
        ).values_list("kind", "observed_at", "updated_at")
    }
    return [
        kind
        for kind in kinds
        # One no sweep has stored cannot be answered, so it is not asked for.
        if kind in attempted
        and kind not in asked
        and askable(kind)
        and freshness(PAGE_VISIT, attempted[kind], now).due
    ]


def _watching(kinds: tuple[str, ...]) -> list[str]:
    """Of ``kinds``, the ones a read request is still waiting on."""

    waiting = {
        kind
        for read in forced_reads()
        for kind in ((read.kind,) if read.kind else read.kinds or ())
    }
    return [kind for kind in kinds if kind in waiting]


def _answer(requested: list[str], watching: list[str]) -> dict[str, Any]:
    return {
        "ok": True,
        "requested": requested,
        "pending": bool(watching),
        "watch": signing.dumps(watching, salt=_WATCH_SALT) if watching else "",
    }


def visit_state(watch: str) -> dict[str, Any] | None:
    """Whether what a page was told to watch for is still being read.

    Asks for nothing and works nothing out: ``watch`` is what the POST handed
    back. None for one HQ did not sign, or signed too long ago.
    """

    try:
        kinds = signing.loads(watch, salt=_WATCH_SALT, max_age=_WATCH_SECONDS)
    except signing.BadSignature:
        return None
    if not isinstance(kinds, list) or not all(isinstance(kind, str) for kind in kinds):
        return None
    return {"ok": True, "pending": bool(_watching(tuple(kinds)))}


def request_visit_refresh(subject: str, name: str, *, principal: Principal) -> dict[str, Any] | None:
    """Ask for the readings behind one page that are due; None for no such page.

    Silent for a principal who cannot ask: the page still renders, and nothing
    is read, or said to be being read, for somebody who could not have pressed
    Read now.
    """

    reads = reads_of(subject, name)
    if reads is None:
        return None
    standing = controller_standing()
    if (
        not standing.known
        or standing.silent
        or not principal.permits(Capability.MANAGE_INFRASTRUCTURE)
    ):
        # Nobody is there to read it, or nobody entitled is asking. Asking
        # would leave a request that forces sweeps when the controller
        # returns, and a page promising a reading that is not coming.
        return _answer([], [])
    with operation_context(
        interface=principal.interface, actor=principal.actor, operation="visit.refresh"
    ):
        requested = list(request_reads(_due(reads.kinds, timezone.now()), principal=principal))
    return _answer(requested, _watching(reads.kinds))
