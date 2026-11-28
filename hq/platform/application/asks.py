"""Asking for work that outlives the request, and saying how it stands.

A request for work that can wait on something outside the process records the
ask (a read request for the controller, a job for the runner) and answers at
once; the work reports where its result is stored. One shape says how it
stands (``Standing``) whichever mechanism does it, drawn from what is stored by
one control (``partials/_ask.html``), which a page asks for again while the
work is live (``Ask.follows``, ``core.live``).
"""

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Annotated, Any, ClassVar, Literal, TypedDict, cast
from urllib.parse import urlsplit

from django.contrib import messages
from django.http import HttpRequest, HttpResponse, QueryDict
from django.shortcuts import redirect
from django.utils import timezone

lazy from hq.domains.control_plane.models import ProviderConnection, ProviderInventory, ReadRequest
lazy from hq.domains.control_plane.providers import registry_label
lazy from hq.domains.control_plane.reading_parts import WHOLE, refused_parts
lazy from hq.platform.core import live
lazy from hq.platform.core.web_refusals import refusal_response

from .describe import Describe, listed
from .moments import duration
from .projection import read_once
from .quoted import Quoted
from .record_errors import Sentence, worded
from .routes import reverse
from .ui import button_classes, ended
lazy from . import cadence
lazy from .action_links import read_now_payload
lazy from .cadence import every_sweep
lazy from .read_requests import forced_kinds, read_request_lifetime
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
# What work whose author said nothing of how it ended states.
UNSTATED = Quoted("")
# What a saved change says of when it reaches the live system.
APPLIES_SHORTLY = "HQ applies it shortly."


class StandingAnswer(TypedDict):
    """How one piece of asked-for work stands."""

    state: Annotated[str, Describe(ended(f"Where the work stands: {listed((IDLE, QUEUED, RUNNING, DONE, FAILED))}"))]
    label: Annotated[str, Describe("Where it stands, in the words a page shows.")]
    tone: Annotated[str, Describe("The tone a page colors that label in; empty while nothing was asked.")]
    live: Annotated[bool, Describe("Whether the work is still under way.")]
    note: Annotated[str, Describe("What the work is doing, or how it ended, in a sentence.")]
    stated: Annotated[
        str, Describe("What the work's own author said of how it ended, as written; empty when it said nothing.")
    ]
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
    """How one piece of asked-for work stands; a capability answers with ``as_json``."""

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
    # What its author said of how it ended (a playbook's failed task), as
    # written: beside ``note``, never in it.
    stated: Quoted = UNSTATED

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
            said["state"],
            said["note"],
            since=since,
            percent=said["percent"],
            word=said["label"],
            url=said["url"],
            stated=Quoted(said["stated"]),
        )

    def as_json(self) -> StandingAnswer:
        return {
            "state": self.state,
            "label": self.label,
            "tone": self.tone,
            "live": self.live,
            "note": self.note,
            "stated": self.stated,
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
    # The read set (``core.live``) its work is drawn from, which the page
    # hears change while the work is live and draws the control again from.
    follows: str = ""
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
    # What tells this control apart from another of the same ask on one page
    # (a panel's), each standing as its own work does.
    of: str = ""
    # A PageAction is a link or a post; this is neither, and says so.
    is_ask = True
    danger = False
    method = "post"
    modal = ""

    def __post_init__(self) -> None:
        # A set nothing pulses for would leave the control under way for good.
        if self.follows:
            live.region(self.follows)

    @property
    def anchor(self) -> str:
        """The control's id on its page, the same each time the page draws it."""

        return f"ask-{hashlib.sha256(f'{self.url} {self.value} {self.of}'.encode()).hexdigest()[:12]}"

    @property
    def look(self) -> str:
        """How its button looks (``ui.BUTTON_LOOKS``)."""

        return " ".join((*(("primary",) if self.primary else ()), *(("compact",) if self.compact else ())))

    @property
    def css(self) -> str:
        return button_classes(self.look)


# Each ask's answer extends AskedAnswer, so each field is described once here.
type AskedMessage = Annotated[
    str, Describe("What asking did, in the sentence a page shows; empty where it says nothing.")
]


class AskedAnswer(TypedDict):
    """What every ask answers, whichever worker does the work."""

    ok: Literal[True]
    standing: StandingAnswer
    message: AskedMessage


@dataclass(frozen=True, slots=True)
class Asked:
    """What an ask answers: how its work stands, and the sentence a page
    without script shows. A capability answers ``answer()``; a page reads it
    back with ``of``."""

    standing: Standing
    message: str = ""

    @classmethod
    def of(cls, said: Mapping[str, Any]) -> Asked:
        """What a capability answered; one held for approval has no work
        under way yet, and its message says so."""

        standing = Standing.of(said["standing"]) if "standing" in said else Standing()
        return cls(standing, str(said.get("message", "")))

    def answer(self) -> AskedAnswer:
        return {"ok": True, "standing": self.standing.as_json(), "message": self.message}


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

    def answered(self, request: HttpRequest) -> HttpResponse:
        """What the page's script is answered with once the ask ran: the page
        it was pressed on, which draws the control from what is stored."""

        return redirect(safe_next(request, fallback=self.back()))

    def post(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        """To the page's script: the page again (``answered``), or a refusal's
        sentence. To a plain form post: back to the page, and the ask's sentence."""

        # A script's ask is answered with the control drawn again, so its
        # answer's sentence is said only to a plain post, and work that failed
        # as it was asked, which nothing stores, is said as a refusal.
        scripted = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        if said := self.cannot():
            if scripted:
                return _said(said, 403)
            messages.error(request, said)
            return redirect(safe_next(request, fallback=self.back()))
        call = (request, self.capability, self.payload())
        target = self.target()
        if scripted:
            answer = run_posted(*call, target=target).answer
            if not answer.get("ok"):
                return refusal_response(answer)
            standing = Asked.of(answer).standing
            return _said(standing.note, 409) if standing.state == FAILED else self.answered(request)
        result = execute_posted(*call, target=target)
        asked = Asked.of(result) if result.get("ok") else Asked(Standing(FAILED, refusal_of(result).message))
        failed = asked.standing.state == FAILED
        if (failed or not result.get("message")) and (note := asked.message or asked.standing.note):
            (messages.error if failed else messages.success)(request, note)
        return redirect(safe_next(request, fallback=self.back()))


def _said(sentence: str, status: int) -> HttpResponse:
    return HttpResponse(sentence, status=status, content_type="text/plain; charset=utf-8")


# ----- Reads the controller takes ---------------------------------------------


def asked_read(kinds: Iterable[str], asked: datetime, *, connection_ref: str = "", message: str = "") -> Asked:
    """What an ask for a read of ``kinds`` (none: every kind) at ``asked`` answers."""

    return Asked(read_standing(tuple(kinds), asked, connection_ref=connection_ref), message)


def read_now_standing(url: str) -> Standing:
    """How the read a Read now control posting to ``url`` asks for stands, from
    the requests it stores: under way while it is, failed with why until it is
    asked again, and idle once it ended well, the page showing what it stored."""

    payload = read_now_payload(QueryDict(urlsplit(url).query))
    refs = cast(list[str], payload.get("connection_refs", []))
    kinds = cast(list[str], payload.get("kinds", []))
    every = bool(payload.get("every_connection"))

    def asks_for(ref: str, kind: str) -> bool:
        if every:
            return not ref and not kind
        return (not kind and ref in refs) or (not ref and kind in kinds)

    asked = max((at for ref, kind, at in read_once(_ASKED, _asked) if asks_for(ref, kind)), default=None)
    if asked is None:
        return Standing()
    forced = (*kinds, *(kind for ref in refs for kind in forced_kinds(ref, "") or ()))
    ref = refs[0] if len(refs) == 1 and not kinds else ""
    standing = read_standing(dict.fromkeys(forced), asked, connection_ref=ref)
    return standing if standing.live or standing.state == FAILED else Standing()


_ASKED = "asks.read_now"


def _asked() -> list[tuple[str, str, datetime]]:
    """Every read a person asked for that may still be answered, read once
    for every control a page draws."""

    since = timezone.now() - read_request_lifetime()
    found = ReadRequest.objects.filter(subject="", requested_at__gte=since)
    return list(found.values_list("connection_ref", "kind", "requested_at"))


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
