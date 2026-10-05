"""Asking for work that outlives the request, and saying how it stands.

A request never does the work an operator asks for when that work can wait on
something outside the process. It records the ask (a read request for the
controller, a job for the runner) and answers at once; the work reports where
its result is stored, and a page watching it asks how it stands.

One shape says how any such work stands (``Standing``), whichever mechanism
does it, so one control (``partials/_ask.html``) and one script behaviour
follow all of it. A status resource answers with ``Standing.as_json``.

What a page may watch is handed to it signed (``watch_token``), so a status
resource reports only on what HQ itself said to watch.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.contrib import messages
from django.core import signing
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone

from .moments import duration

IDLE = "idle"
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
LIVE = frozenset({QUEUED, RUNNING})

_LABELS = {IDLE: "", QUEUED: "Queued", RUNNING: "Running", DONE: "Done", FAILED: "Failed"}

_WATCH_SALT = "hq.platform.application.asks.watch"
# Longer than any read request lives, so a page left open can still learn that
# its request went unanswered.
WATCH_SECONDS = 60 * 60
# Generous for a signed list of kinds, and far short of a request line's limit.
WATCH_LIMIT = 4000


@dataclass(frozen=True)
class Standing:
    """How one piece of asked-for work stands."""

    state: str = IDLE
    # What it is doing, or how it ended, in a sentence.
    note: str = ""
    # When it was asked for.
    since: datetime | None = None
    # How far along, where the work knows.
    percent: int | None = None

    @property
    def live(self) -> bool:
        return self.state in LIVE

    @property
    def label(self) -> str:
        return _LABELS[self.state]

    @property
    def seconds(self) -> int:
        if self.since is None:
            return 0
        return max(0, int((timezone.now() - self.since).total_seconds()))

    def as_json(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "label": self.label,
            "live": self.live,
            "note": self.note,
            "seconds": self.seconds,
            "percent": self.percent,
        }


@dataclass(frozen=True)
class Ask:
    """One "do this now" control: what its button posts, and how the work it
    last asked for stands.

    Usable wherever a ``PageAction`` is, and on its own in a page's body.
    ``status_url`` is the resource that says how the work stands while it is
    live. ``refresh`` is a CSS selector for the part of the page the result
    shows in, which is fetched again when the work ends; without one the page
    is loaded again.
    """

    label: str
    url: str
    standing: Standing = field(default_factory=Standing)
    status_url: str = ""
    refresh: str = ""
    name: str = "action"
    value: str = ""
    title: str = ""
    primary: bool = False
    compact: bool = False
    # Shown but not usable, with ``title`` saying why.
    disabled: bool = False
    # A PageAction is a link or a post; this is neither, and says so.
    is_ask = True
    danger = False
    method = "post"
    modal = ""

    @property
    def css(self) -> str:
        return " ".join(
            ("btn", *(("primary",) if self.primary else ()), *(("compact",) if self.compact else ()))
        )


def answer(
    request: HttpRequest,
    standing: Standing,
    *,
    fallback: str,
    status_url: str = "",
    message: str = "",
) -> HttpResponse:
    """Answer the request that asked, at once.

    To the page's script: how the work stands and where to watch it, 202 while
    it is live (RFC 9110 15.3.3). To a plain form post: back to the page it
    came from, which says the same from what is stored. ``message`` is the
    sentence a page without script shows; the standing's note otherwise.
    """

    from .security import safe_next

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse(
            {**standing.as_json(), "status": status_url if standing.live else ""},
            status=202 if standing.live else 200,
        )
    said = message or standing.note
    if said:
        (messages.error if standing.state == FAILED else messages.success)(request, said)
    return redirect(safe_next(request, fallback=fallback))


def watch_token(subject: dict[str, Any]) -> str:
    """What a page may ask the status of, signed."""

    return signing.dumps(subject, salt=_WATCH_SALT)


def watched(token: str) -> dict[str, Any] | None:
    """The subject of a token HQ signed, or None for anything else."""

    try:
        found = signing.loads(token, salt=_WATCH_SALT, max_age=WATCH_SECONDS)
    except signing.BadSignature:
        return None
    return found if isinstance(found, dict) else None


# ----- Reads the controller takes ---------------------------------------------


def read_watch(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "") -> str:
    """The token for a read of ``kinds`` (none: every kind) asked for at ``asked``."""

    return watch_token(
        {"read": sorted(set(kinds)), "ref": connection_ref, "asked": asked.isoformat()}
    )


def read_status_url(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "") -> str:
    token = read_watch(kinds, asked, connection_ref=connection_ref)
    return f"{reverse('control_plane:read_status')}?watch={token}"


def read_standing(
    kinds: Iterable[str], asked: datetime, *, connection_ref: str = ""
) -> Standing:
    """How a read the controller was asked for at ``asked`` stands.

    Three stored facts decide it and nothing is asked of anyone: when each kind
    was last tried, when the connection was last probed, and when the
    controller last arrived.
    """

    from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
    from hq.domains.control_plane.reading_parts import WHOLE, refused_parts

    from .cadence import controller_standing, every_sweep, read_request_lifetime

    wanted = tuple(sorted(set(kinds)))
    found = ProviderInventory.objects.defer("records")
    snapshots = {
        snapshot.kind: snapshot
        for snapshot in (found.filter(kind__in=wanted) if wanted else found)
        # No kinds named is every kind a sweep reads.
        if wanted or every_sweep(snapshot.kind)
    }
    tried = bool(snapshots) and all(
        kind in snapshots and snapshots[kind].updated_at > asked for kind in wanted or snapshots
    )
    if connection_ref:
        probed = max(
            ProviderConnection.objects.filter(connection_ref=connection_ref).values_list(
                "observed_at", flat=True
            ),
            default=None,
        )
        tried = tried and probed is not None and probed > asked
    if not tried:
        if timezone.now() - asked > read_request_lifetime():
            return Standing(
                FAILED,
                f"No controller answered within {duration(read_request_lifetime())}.",
                since=asked,
            )
        seen = controller_standing().seen_at
        if seen is not None and seen > asked:
            return Standing(RUNNING, "The controller is reading it.", since=asked)
        return Standing(QUEUED, "Waiting for the controller.", since=asked)
    failed = [snapshot for snapshot in snapshots.values() if not snapshot.reachable]
    if failed:
        first = failed[0]
        return Standing(FAILED, first.error or f"{first.kind} could not be read.", since=asked)
    refused = [
        refusal
        for snapshot in snapshots.values()
        for refusal in refused_parts(snapshot)
        if refusal.part.name == WHOLE and not refusal.scope
    ]
    if refused:
        return Standing(FAILED, f"{refused[0].phrase}.", since=asked)
    return Standing(DONE, "Read just now.", since=asked)


# ----- Jobs the runner runs ---------------------------------------------------


def job_standing(job: Any) -> Standing:
    """How a ``jobs.Job`` stands, in the shared shape."""

    states = {"queued": QUEUED, "running": RUNNING, "succeeded": DONE}
    state = states.get(job.state, FAILED)
    note = job.note
    if state == FAILED:
        note = job.error.strip().splitlines()[-1] if job.error else "The job did not finish."
    elif state == QUEUED and not note:
        note = "Starting…"
    return Standing(state, note, since=job.created_at, percent=job.percent if state != DONE else 100)
