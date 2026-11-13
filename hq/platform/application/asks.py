"""Asking for work that outlives the request, and saying how it stands.

A request for work that can wait on something outside the process records the
ask (a read request for the controller, a job for the runner) and answers at
once; the work reports where its result is stored, and a page watching it asks
how it stands. One shape says so (``Standing``) whichever mechanism does it, so
one control (``partials/_ask.html``) and one script behavior follow all of it.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, TypedDict

from django.contrib import messages
from django.core import signing
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone

lazy from hq.domains.control_plane.models import OperationState, ProviderConnection, ProviderInventory
lazy from hq.domains.control_plane.providers import registry_label
lazy from hq.domains.control_plane.reading_parts import WHOLE, refused_parts
lazy from hq.domains.jobs.models import JobState

from .describe import Describe, listed
from .moments import duration
from .routes import reverse
from .ui import ended
lazy from . import cadence
lazy from .cadence import every_sweep, read_request_lifetime
lazy from .resource_operations import headline as operation_headline
lazy from .security import safe_next
lazy from .work_runs import LAPSED

IDLE = "idle"
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
LIVE = frozenset({QUEUED, RUNNING})

# A read's state in the words ``jobs.JobState`` says a job's in, so no two
# pages call the same state two things. A job's standing carries its own.
_LABELS = {IDLE: "", QUEUED: "Waiting", RUNNING: "Running", DONE: "Done", FAILED: "Failed"}

# What asked-for work says while it waits its turn, and while a read is under way.
WAITING = "Waiting…"
REFRESHING = "Refreshing…"
# What a read says once it is in, and what a page says of a newer reading it
# could not draw in place, with the link that loads it.
REFRESHED = "Refreshed just now."
NEWER_READING = "A newer reading is in."
SHOW_NEWER = "Show the reading"
# What a read asked of the controller says when none is arriving to take it;
# while one is on its way, it says REFRESHING.
READ_UNTAKEN = "Cannot refresh: {absent}"
# What a saved change says of when it reaches the live system.
APPLIES_SHORTLY = "HQ applies it shortly."

_WATCH_SALT = "hq.platform.application.asks.watch"
# Longer than any read request lives, so a page left open can still learn that
# its request went unanswered.
WATCH_SECONDS = 60 * 60
# Generous for a signed list of kinds, and far short of a request line's limit.
WATCH_LIMIT = 4000


class StandingAnswer(TypedDict):
    """How one piece of asked-for work stands."""

    state: Annotated[str, Describe(ended(f"Where the work stands: {listed((IDLE, QUEUED, RUNNING, DONE, FAILED))}"))]
    label: Annotated[str, Describe("Where it stands, in the words a page shows.")]
    live: Annotated[bool, Describe("Whether the work is still under way.")]
    note: Annotated[str, Describe("What the work is doing, or how it ended, in a sentence.")]
    seconds: Annotated[int, Describe("How long ago the work was asked for, in whole seconds; 0 when it was not.")]
    percent: Annotated[int | None, Describe("How far along the work is, where it knows; null where it does not.")]


def _bare(said: str) -> str:
    return said.strip().rstrip(".…").strip().casefold()


def restates(sentence: str, word: str) -> bool:
    """Whether ``sentence`` says nothing ``word`` does not, its stops aside."""

    return bool(sentence) and _bare(sentence) == _bare(word)


@dataclass(frozen=True, slots=True)
class Standing:
    """How one piece of asked-for work stands; a status resource answers with ``as_json``."""

    state: str = IDLE
    # What it is doing, or how it ended, in a sentence.
    note: str = ""
    # When it was asked for.
    since: datetime | None = None
    # How far along, where the work knows.
    percent: int | None = None
    # The state in words, where the work's own record has them.
    word: str = ""

    def __post_init__(self) -> None:
        # Every caller says how its work stands through here, so a sentence
        # that only says the state's word again ("Waiting" and "Waiting…") is
        # dropped once, for every control, and the word stands alone.
        if restates(self.note, self.label):
            object.__setattr__(self, "note", "")

    @property
    def live(self) -> bool:
        return self.state in LIVE

    @property
    def label(self) -> str:
        return self.word or _LABELS[self.state]

    @property
    def seconds(self) -> int:
        if self.since is None:
            return 0
        return max(0, int((timezone.now() - self.since).total_seconds()))

    def as_json(self) -> StandingAnswer:
        return {
            "state": self.state,
            "label": self.label,
            "live": self.live,
            "note": self.note,
            "seconds": self.seconds,
            "percent": self.percent,
        }


@dataclass(frozen=True, slots=True)
class Ask:
    """One "do this now" control: what its button posts, and how the work it
    last asked for stands. Usable wherever a ``PageAction`` is, and on its own
    in a page's body.
    """

    label: str
    url: str
    standing: Standing = field(default_factory=Standing)
    # The resource that says how the work stands while it is live.
    status_url: str = ""
    # A CSS selector for the part of the page the result shows in, fetched
    # again when the work ends; without one, the page's content.
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
        return " ".join(("btn", *(("primary",) if self.primary else ()), *(("compact",) if self.compact else ())))


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
    """What a page may ask the status of, signed, so a status resource reports
    only on what HQ itself said to watch.
    """

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

    return watch_token({"read": sorted(set(kinds)), "ref": connection_ref, "asked": asked.isoformat()})


def read_status_url(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "") -> str:
    token = read_watch(kinds, asked, connection_ref=connection_ref)
    return f"{reverse('control_plane:read_status')}?watch={token}"


def _untried(asked: datetime) -> Standing:
    """How a read asked for at ``asked``, and not yet answered, stands: under
    way once the controller has arrived since, waiting while one is due, and
    failed when none is arriving to take it. Readings in a database copied from
    elsewhere are no controller of this HQ's (``ControllerStanding.here``)."""

    controller = cadence.controller_standing()
    if controller.seen_at is None or not controller.here:
        return Standing(FAILED, ended(READ_UNTAKEN.format(absent="no controller reports to this HQ")), since=asked)
    if controller.seen_at > asked:
        return Standing(RUNNING, REFRESHING, since=asked)
    if controller.silent:
        silence = duration(timezone.now() - controller.seen_at)
        return Standing(FAILED, ended(READ_UNTAKEN.format(absent=f"no controller seen in {silence}")), since=asked)
    return Standing(QUEUED, REFRESHING, since=asked)


def read_standing(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "") -> Standing:
    """How a read the controller was asked for at ``asked`` stands.

    Three stored facts decide it and nothing is asked of anyone: when each kind
    was last tried, when the connection was last probed, and when the
    controller last arrived.
    """

    wanted = tuple(sorted(set(kinds)))
    found = ProviderInventory.objects.defer("records")
    snapshots = {
        snapshot.kind: snapshot
        # No kinds named is every kind a sweep reads.
        for snapshot in (found.filter(kind__in=wanted) if wanted else found)
        if wanted or every_sweep(snapshot.kind)
    }
    tried = bool(snapshots) and all(
        kind in snapshots and snapshots[kind].updated_at > asked for kind in wanted or snapshots
    )
    if connection_ref:
        probed = max(
            ProviderConnection.objects.filter(connection_ref=connection_ref).values_list("observed_at", flat=True),
            default=None,
        )
        tried = tried and probed is not None and probed > asked
    if not tried:
        if timezone.now() - asked > read_request_lifetime():
            return Standing(
                FAILED,
                f"No answer within {duration(read_request_lifetime())}.",
                since=asked,
            )
        return _untried(asked)
    failed = [snapshot for snapshot in snapshots.values() if not snapshot.reachable]
    if failed:
        first = failed[0]
        return Standing(FAILED, first.error or f"{registry_label(first.kind)} could not be read.", since=asked)
    refused = [
        refusal
        for snapshot in snapshots.values()
        for refusal in refused_parts(snapshot)
        if refusal.part.name == WHOLE and not refusal.scope
    ]
    if refused:
        return Standing(FAILED, ended(refused[0].phrase), since=asked)
    return Standing(DONE, REFRESHED, since=asked)


# ----- Jobs the runner runs ---------------------------------------------------


# How work stands by the state it is seen in, as ``jobs.JobState`` says it,
# so a job and an operation a worker carries out read alike; any other has
# ended badly.
_STANDS = {"queued": QUEUED, "running": RUNNING, "succeeded": DONE}


def job_standing(job: Any) -> Standing:
    """How a ``jobs.Job`` stands, in the shared shape."""

    state = _STANDS.get(job.seen_state, FAILED)
    note = job.seen_error if state == FAILED else job.note
    return Standing(
        state,
        note,
        since=job.created_at,
        percent=job.percent if state != DONE else 100,
        word=job.seen_state_display,
    )


# ----- Operations a worker carries out ----------------------------------------


def operation_standing(operation: Any) -> Standing:
    """How an ``OperationRequest`` stands, in the shared shape: by the state
    Jobs shows it in, which its query annotates as ``seen``
    (``work_runs.run_seen_state``), and in the sentence its history says."""

    seen = JobState(operation.seen)
    lapsed = seen == JobState.LOST and operation.state == OperationState.CLAIMED
    note = LAPSED if lapsed else operation_headline(operation)
    return Standing(_STANDS.get(seen, FAILED), note, since=operation.created_at, word=seen.label)
