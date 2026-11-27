"""Asking for work that outlives the request, and saying how it stands.

A request for work that can wait on something outside the process records the
ask (a read request for the controller, a job for the runner) and answers at
once; the work reports where its result is stored, and a page watching it asks
how it stands. One shape says so (``Standing``) whichever mechanism does it, so
one control (``partials/_ask.html``) and one script behavior follow all of it.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, ClassVar, Literal, TypedDict

from django.contrib import messages
from django.core import signing
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View

from hq.platform.application.url_query import with_query
lazy from hq.domains.control_plane.models import ProviderConnection, ProviderInventory
lazy from hq.domains.control_plane.providers import registry_label
lazy from hq.domains.control_plane.reading_parts import WHOLE, refused_parts

from .describe import Describe, listed
from .moments import duration
from .record_errors import Sentence, worded
from .routes import reverse
from .ui import button_classes, ended
lazy from . import cadence
lazy from .cadence import every_sweep
lazy from .moments import moment
lazy from .read_requests import read_request_lifetime
lazy from .refusal_status import refusal_of
lazy from .security import safe_next
lazy from .web_writes import execute_posted, run_posted
lazy from .work_runs import standing_tone, standing_word

IDLE = "idle"
QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
LIVE = frozenset({QUEUED, RUNNING})

# What asked-for work says while it waits its turn, and while a read is under way.
WAITING = "Waiting…"
REFRESHING = "Refreshing…"
# What a read says once it is in, and what a page says of a newer reading it
# could not draw in place, with the link that loads it.
REFRESHED = "Refreshed just now."
NEWER_READING = "A newer reading is in."
SHOW_NEWER = "Show the reading"
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
    tone: Annotated[str, Describe("The tone a page colors that label in; empty while nothing was asked.")]
    live: Annotated[bool, Describe("Whether the work is still under way.")]
    note: Annotated[str, Describe("What the work is doing, or how it ended, in a sentence.")]
    seconds: Annotated[int, Describe("How long ago the work was asked for, in whole seconds; 0 when it was not.")]
    percent: Annotated[int | None, Describe("How far along the work is, where it knows; null where it does not.")]
    url: Annotated[str, Describe("The work's own page: a playbook run's or a scan's; empty where it has none.")]


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
    # The state in words, where the work's own record has them; else the
    # words its job state is said in (``work_runs.standing_word``).
    word: str = ""
    # The work's own page, where it has one.
    url: str = ""

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
        return self.word or standing_word(self.state)

    @property
    def tone(self) -> str:
        """The tone its label is colored in, the one the page and the script draw; "" while idle."""

        return standing_tone(self.state, self.label)

    @property
    def said(self) -> str:
        """Its label and its sentence, as one line says them."""

        return f"{self.label}: {self.note}" if self.note else self.label

    @property
    def seconds(self) -> int:
        if self.since is None:
            return 0
        return max(0, int((timezone.now() - self.since).total_seconds()))

    @classmethod
    def of(cls, said: StandingAnswer) -> Standing:
        """The standing a command answered with ``as_json``."""

        since = timezone.now() - timedelta(seconds=said["seconds"]) if said["seconds"] else None
        return cls(
            said["state"], said["note"], since=since, percent=said["percent"], word=said["label"], url=said["url"]
        )

    def as_json(self) -> StandingAnswer:
        return {
            "state": self.state,
            "label": self.label,
            "tone": self.tone,
            "live": self.live,
            "note": self.note,
            "seconds": self.seconds,
            "percent": self.percent,
            "url": self.url,
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
    # The ``refresh`` part draws how the work ended, failure included.
    outcome: bool = False
    # A PageAction is a link or a post; this is neither, and says so.
    is_ask = True
    danger = False
    method = "post"
    modal = ""

    @property
    def look(self) -> str:
        """How its button looks (``ui.BUTTON_LOOKS``)."""

        return " ".join((*(("primary",) if self.primary else ()), *(("compact",) if self.compact else ())))

    @property
    def css(self) -> str:
        return button_classes(self.look)


# Each ask's answer extends AskedAnswer, so each field is described once here.
type AskedStatusUrl = Annotated[str, Describe("Where to follow the work while it is under way; empty when nothing is.")]
type AskedMessage = Annotated[
    str, Describe("What asking did, in the sentence a page shows; empty where it says nothing.")
]


class AskedAnswer(TypedDict):
    """What every ask answers, whichever worker does the work."""

    ok: Literal[True]
    standing: StandingAnswer
    status_url: AskedStatusUrl
    message: AskedMessage


@dataclass(frozen=True, slots=True)
class Asked:
    """What an ask answers: how its work stands, where a page watches it while
    it is live, and the sentence a page without script shows. A capability
    answers ``answer()``; a page reads it back with ``of``."""

    standing: Standing
    status_url: str = ""
    message: str = ""

    @classmethod
    def of(cls, said: Mapping[str, Any]) -> Asked:
        """What a capability answered; one held for approval has no work
        under way yet, and its message says so."""

        standing = Standing.of(said["standing"]) if "standing" in said else Standing()
        return cls(standing, str(said.get("status_url", "")), str(said.get("message", "")))

    def answer(self) -> AskedAnswer:
        return {"ok": True, "standing": self.standing.as_json(), "status_url": self.status_url, "message": self.message}


# The mode an ask's route runs its command in (``pages.Runs``): one press,
# answered at once. Only the ask's own button is sent there; any other control
# for the command still opens the command's form.
ASK = "ask"


class AskMixin:
    """The view an ``Ask``'s button posts to, declared by what it asks: it
    runs ``capability`` as the person who pressed it and answers at once, a
    refusal as failed work in the refusal's own sentence."""

    http_method_names: ClassVar[list[str]] = ["post"]
    capability: str
    back_to: str

    @property
    def runs(self) -> tuple[tuple[str, str], ...]:
        return ((self.capability, ASK),)

    def back(self) -> str:
        return reverse(self.back_to)

    def payload(self) -> dict[str, Any]:
        return {}

    def target(self) -> str | None:
        return None

    def cannot(self) -> Sentence:
        """Why the person cannot ask at all, before anything runs; "" when they can."""

        return worded("")

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """To the page's script: how the work stands and where to watch it, 202
        while it is live (RFC 9110 15.3.3). To a plain form post: back to the
        page, which says the same from what is stored, and the ask's sentence."""

        # A script's ask is answered in JSON and draws no page, so its answer's
        # sentence is said only to a plain post, and then in place of the ask's.
        scripted = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        result: dict[str, Any] = {}
        if said := self.cannot():
            asked = Asked(Standing(FAILED, said))
        else:
            call = (request, self.capability, self.payload())
            target = self.target()
            result = run_posted(*call, target=target).answer if scripted else execute_posted(*call, target=target)
            asked = Asked.of(result) if result.get("ok") else Asked(Standing(FAILED, refusal_of(result).message))
        standing = asked.standing
        if scripted:
            return JsonResponse(
                {**standing.as_json(), "status": asked.status_url if standing.live else ""},
                status=202 if standing.live else 200,
            )
        failed = standing.state == FAILED
        if (failed or not result.get("message")) and (note := asked.message or standing.note):
            (messages.error if failed else messages.success)(request, note)
        return redirect(safe_next(request, fallback=self.back()))


class WatchView(View):
    """How asked-for work stands, for a page following it: only what HQ signed
    for that page to watch (``watch_token``), by default a read of kinds."""

    http_method_names: ClassVar[list[str]] = ["get"]

    def standing(self, subject: dict[str, Any]) -> Standing | None:
        asked = moment(str(subject.get("asked", "")))
        if asked is None or not isinstance(subject.get("read"), list):
            return None
        return read_standing(map(str, subject["read"]), asked, connection_ref=str(subject.get("ref", "")))

    def get(self, request: HttpRequest) -> HttpResponse:
        token = str(request.GET.get("watch", ""))
        found = watched(token) if 0 < len(token) <= WATCH_LIMIT else None
        if (standing := self.standing(found) if found else None) is None:
            raise Http404("Nothing to watch.")
        return JsonResponse(standing.as_json())


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


def asked_read(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "", message: str = "") -> Asked:
    """What an ask for a read of ``kinds`` (none: every kind) at ``asked`` answers."""

    wanted = tuple(kinds)
    return Asked(
        read_standing(wanted, asked, connection_ref=connection_ref),
        read_status_url(wanted, asked, connection_ref=connection_ref),
        message,
    )


def read_status_url(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "") -> str:
    """Where a page follows a read of ``kinds`` (none: every kind) asked for at ``asked``."""

    token = watch_token({"read": sorted(set(kinds)), "ref": connection_ref, "asked": asked.isoformat()})
    return with_query(reverse("control_plane:read_status"), {"watch": token})


def _untried(asked: datetime) -> Standing:
    """How a read asked for at ``asked``, and not yet answered, stands: under
    way once the controller has arrived since, waiting while one is due, and
    failed when none is arriving to take it. Readings in a database copied from
    elsewhere are no controller of this HQ's (``WorkerStanding.here``)."""

    controller = cadence.controller_standing()
    if controller.here and controller.seen_at is not None and controller.seen_at > asked:
        return Standing(RUNNING, REFRESHING, since=asked)
    if untaken := controller.cannot("refresh", now=timezone.now()):
        return Standing(FAILED, untaken, since=asked)
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
