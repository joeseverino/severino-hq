"""Work that reaches outside the process, declared once.

A request never waits on a network, a process or a timer
(``hq.platform.core.outbound``), so a domain declares such work as an
``OutboundWork``: a function called on a job's own thread with a ``Progress``,
the subject and the principal that asked. Its job, capability, route, control
and audit entries derive from that one declaration (docs/Plugins.md#outbound-work).
"""

import inspect
from annotationlib import Format
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Annotated, Any, NotRequired, cast

from django.core.exceptions import ImproperlyConfigured
lazy from django.utils import timezone

lazy from hq.domains.jobs.models import Job
lazy from hq.domains.jobs.runner import ALREADY_RUNNING, SCHEDULER, JobConflict, Progress, due as is_due, run, start

from .asks import FAILED, Ask, Asked, AskedAnswer, Standing
from .contracts import DOTTED_NAME, Effect
from .describe import Describe
from .integration_specs import CapabilitySpec, requested
from .process_caches import kept
from .projection import read_once
from .record_errors import RecordNotFound
from .refusal_codes import Code
from .refusal_status import RefusalEnvelope, refuse
from .security import WAITING_INTERFACES, Principal, internal_principal, person_of
from .ui import ended
lazy from .domains import host_specs
lazy from .plugins import Composition, composition, plugin_outbound_work
lazy from .routes import reverse
lazy from .work_runs import job_run, standing_of

# ``jobs.Job.kind`` holds the name.
NAME_LIMIT = 64
# The job's label, and the button's words.
LABEL_LIMIT = 60
# The field a control posts its subject in.
SUBJECT_FIELD = "subject"
ANOTHER_RUNNING = "Busy with another one. Try again when it ends."


@dataclass(frozen=True, slots=True)
class OutboundWork:
    """One piece of work a domain does outside the process.

    Every control and answer for the work reads its words from here. Work about
    one record names it in ``subject_label``; work about none may declare
    ``every`` so the host's ``due`` timer keeps its data current.
    """

    # The job's kind, the capability's name and the last segment of the route.
    name: str
    # What the job, the audit entry and the command are called.
    label: str
    summary: str
    required_capability: str
    # Called as run(progress, subject=, principal=); returns what the job stores
    # as its result, or raises jobs.Failed with a sentence saying why it failed.
    run: Callable[..., dict[str, Any] | None]
    # What the one record is called; set, the subject is required and is the
    # capability's target.
    subject_label: str = ""
    subject_help: str = ""
    subject_resource: str | None = None
    # Why the work cannot be asked for a subject now, from what HQ holds ("" when
    # it can): the control is drawn unusable with it, and an ask is refused with
    # it before any job is recorded.
    refuse: Callable[[str], str] | None = None
    # A change somewhere else, never a read of HQ; given as its value's string too.
    effect: Effect = Effect.REMOTE_WRITE
    execution_notes: tuple[str, ...] = ()
    # For work about no one record: once this long has passed since it last
    # succeeded, the host's due timer does it.
    every: timedelta | None = None
    # What its button says where that is shorter than the label ("Refresh").
    verb: str = ""
    # False for work only a timer or HQ itself asks for: no capability, control
    # or route offers it, so it needs no effect or required capability. The
    # host's own (``host_work``) is also what its units ask for by name.
    offered: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "effect", Effect(self.effect))

    @property
    def takes_subject(self) -> bool:
        return bool(self.subject_label)

    @property
    def words(self) -> str:
        """What its button says."""

        return self.verb or self.label


@dataclass(frozen=True, slots=True)
class _NoArguments:
    """What is acted on is the target; there is nothing else to say."""


def validate(work: object) -> OutboundWork:
    """Refuse a declaration HQ could not run, when the composition loads."""

    if not isinstance(work, OutboundWork):
        raise ImproperlyConfigured("Outbound work is declared as OutboundWork.")
    if not DOTTED_NAME.fullmatch(work.name) or len(work.name) > NAME_LIMIT:
        raise ImproperlyConfigured(f"Invalid outbound work name {work.name!r}.")
    if not work.label or any(text != text.strip() or len(text) > LABEL_LIMIT for text in (work.label, work.verb)):
        raise ImproperlyConfigured(f"Outbound work {work.name!r} needs a short label.")
    if work.offered and not work.effect.changes:
        raise ImproperlyConfigured(f"Outbound work {work.name!r} reads only, so it is no outbound work.")
    if work.refuse is not None and not callable(work.refuse):
        raise ImproperlyConfigured(f"Outbound work {work.name!r} refuse is not callable.")
    # Checked as what was given: a declaration does not have to respect the annotation.
    every: object = work.every
    if every is not None:
        if not isinstance(every, timedelta) or every <= timedelta(0):
            raise ImproperlyConfigured(
                f"Outbound work {work.name!r} declares an every that is not a positive timedelta."
            )
        if work.takes_subject:
            raise ImproperlyConfigured(
                f"Outbound work {work.name!r} is about one record, so nothing can be due on a cadence."
            )
    try:
        inspect.signature(work.run, annotation_format=Format.STRING).bind(None, subject="", principal=None)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"Outbound work {work.name!r} must be run(progress, *, subject, principal)."
        ) from exc
    return work


@kept
def _declared(_composition: Composition) -> dict[str, OutboundWork]:
    declared: dict[str, OutboundWork] = {}
    # The host's domains declare theirs as an extension does, so neither can
    # take a name the other holds.
    for work in (*host_specs("outbound"), *plugin_outbound_work()):
        validate(work)
        if work.name in declared:
            raise ImproperlyConfigured(f"Duplicate outbound work {work.name!r}.")
        declared[work.name] = work
    return declared


@kept
def _host() -> frozenset[str]:
    return frozenset(work.name for work in host_specs("outbound") if not work.offered)


def declared_work() -> dict[str, OutboundWork]:
    """Every piece of outbound work the composition declares, by name."""

    return _declared(composition())


def host_work() -> tuple[OutboundWork, ...]:
    """The host's own work, which nobody is offered: what its timer and units
    ask for by name."""

    return tuple(work for name, work in declared_work().items() if name in _host())


def job_names() -> dict[str, str]:
    """Each declared job's name in words, by its kind."""

    return {name: work.label for name, work in declared_work().items()}


def capability_specs() -> tuple[CapabilitySpec, ...]:
    """The capability each piece of offered work is asked for through."""

    return tuple(capability_for(work) for work in declared_work().values() if work.offered)


def capability_for(work: OutboundWork) -> CapabilitySpec:
    """The capability an adapter asks for the work through."""

    validate(work)
    if not work.offered:
        raise ImproperlyConfigured(f"Outbound work {work.name!r} is offered to nobody, so no capability asks for it.")

    def about(_command: Any, *, principal: Principal, current_key: str = "") -> WorkRequested | RefusalEnvelope:
        return request_work(work, current_key, principal=principal)

    def whole(_command: Any, *, principal: Principal) -> WorkRequested | RefusalEnvelope:
        return request_work(work, "", principal=principal)

    return CapabilitySpec(
        work.name,
        work.summary,
        work.effect,
        work.required_capability,
        _NoArguments,
        about if work.takes_subject else whole,
        "key" if work.takes_subject else None,
        subject_resource=work.subject_resource,
        target_label=work.subject_label,
        target_help=work.subject_help,
        execution_notes=(
            *work.execution_notes,
            "Runs in the background, one at a time.",
        ),
        label=work.label,
        verb=work.verb,
    )


def _refusal(work: OutboundWork, subject: str) -> str:
    return (work.refuse(subject) if work.refuse else "") or ""


def refusal(name: str, subject: str = "") -> str:
    """Why the work named ``name`` would be refused for ``subject`` now, or "".

    What a link to the work reads, so it is drawn unusable before a press
    rather than refused after one. "" for a name no work declares, and for
    work about one record asked of none: that link chooses its record, and the
    choice is answered when it is made. A refusal may read the database, so a
    request asks each one once however many rows offer it.
    """

    work = declared_work().get(name)
    if work is None or work.refuse is None or (work.takes_subject and not subject):
        return ""

    def answer() -> str:
        # A refusal may say a record is missing as a capability does, by
        # raising; the link says the same sentence.
        try:
            return _refusal(work, subject)
        except RecordNotFound as exc:
            return str(exc)

    return read_once(f"outbound_work.refusal:{name}:{subject}", answer)


class WorkRequested(AskedAnswer):
    """Work asked of HQ, and the job doing it."""

    started: Annotated[bool, Describe("Whether this call started the work; false when it was already running.")]
    job: NotRequired[Annotated[str, Describe("The id of the job doing it; absent when another job holds the work.")]]
    result: NotRequired[
        Annotated[
            dict[str, Any],
            Describe("What the work answered, where the call waited for it to end, as a command line does."),
        ]
    ]


def _job_asked(job: Job, message: str) -> AskedAnswer:
    """How the job doing asked-for work stands, and where a page follows it."""

    return Asked(standing_of(job_run(job)), reverse("jobs:status", args=[job.pk]), message).answer()


def request_work(work: OutboundWork, subject: str, *, principal: Principal) -> WorkRequested | RefusalEnvelope:
    """Ask for the work and answer at once, as a capability answers.

    A caller with a request to answer gets a job on its own thread. A command
    line has no thread that outlives it, so there the work runs to its end
    before the answer. Reached only as a capability, which has authorized the
    caller.
    """

    reason = _refusal(work, subject)
    if reason:
        return cast(RefusalEnvelope, refuse(Code.PRECONDITION, reason))
    waited = principal.interface in WAITING_INTERFACES
    try:
        job = (run if waited else start)(
            work.name,
            work.label,
            _job_work(work, subject, principal),
            actor=principal.actor,
            requested_by=person_of(principal),
            request={SUBJECT_FIELD: subject},
        )
    except JobConflict:
        live = Job.objects.filter(kind=work.name, state__in=("queued", "running")).first()
        if live is not None and live.request.get(SUBJECT_FIELD, "") == subject:
            return {**_job_asked(live, ALREADY_RUNNING), "started": False, "job": str(live.pk)}
        # Another subject's run of the same work holds the one live slot.
        return {**Asked(Standing(FAILED, ANOTHER_RUNNING), message=ANOTHER_RUNNING).answer(), "started": False}
    if not waited:
        return {**_job_asked(job, requested(work.words, subject)), "started": True, "job": str(job.pk)}
    standing = standing_of(job_run(job))
    if standing.state == FAILED:
        return cast(RefusalEnvelope, refuse(Code.WORK_FAILED, standing.note or ended(standing.label)))
    return {**_job_asked(job, standing.note), "started": True, "job": str(job.pk), "result": job.result}


def run_now(name: str, subject: str = "", *, principal: Principal) -> Job:
    """Do declared work to its end on the calling thread, and return its job.

    For a command a timer runs: the same job row, one-at-a-time rule and audit
    entry as a pressed button. Refused inside a request.
    """

    work = _named(name)
    if work.offered:
        principal.require(work.required_capability)
    reason = _refusal(work, subject)
    if reason:
        raise ValueError(reason)
    return run(
        work.name,
        work.label,
        _job_work(work, subject, principal),
        actor=principal.actor,
        request={SUBJECT_FIELD: subject},
    )


def _job_work(work: OutboundWork, subject: str, principal: Principal) -> Callable[[Progress], dict[str, Any]]:
    def job_work(progress: Progress) -> dict[str, Any]:
        return work.run(progress, subject=subject, principal=principal) or {}

    return job_work


def _named(name: str) -> OutboundWork:
    work = declared_work().get(name)
    if work is None:
        raise LookupError(f"No outbound work named {name!r} is declared.")
    return work


def _host_named(name: str) -> OutboundWork:
    """The host's own work by name. Nothing else is done for a unit or for HQ's
    own start, so a caller naming work cannot reach an extension's or an
    offered capability's."""

    if name not in _host():
        raise ValueError(f"No host work named {name!r} is declared.")
    return _named(name)


def _scheduler() -> Principal:
    return internal_principal(SCHEDULER)


def run_for_host(name: str) -> dict[str, Any]:
    """Do the host's own work a unit asked for to its end, and say how it ended.

    A live job of the same kind is the answer, not a failure: the work is
    being done.
    """

    work = _host_named(name)
    try:
        job = run_now(work.name, principal=_scheduler())
    except JobConflict:
        return {"name": name, "state": "running", "note": ALREADY_RUNNING}
    return {"name": name, "state": job.state, "note": job_run(job).note, "job": str(job.pk)}


def start_for_host(name: str) -> bool:
    """Start the host's own work on a thread of its own, for HQ that learned it
    is due sooner than the timer would find it; False when it is already running."""

    work = _host_named(name)
    principal = _scheduler()
    try:
        start(
            work.name,
            work.label,
            _job_work(work, "", principal),
            actor=principal.actor,
            request={SUBJECT_FIELD: ""},
        )
    except JobConflict:
        return False
    return True


def due(now: datetime | None = None) -> tuple[OutboundWork, ...]:
    """The declared work whose cadence has come round: never done, or last
    done well longer ago than its ``every``, and not being done now."""

    now = now or timezone.now()
    return tuple(
        work
        for work in declared_work().values()
        if work.every is not None and is_due(work.name, work.every, now=now, subject="")
    )


def last_job(name: str, subject: str = "") -> Any:
    """The job that last did this work for this subject, or None. One query."""

    return Job.objects.filter(kind=name, **{f"request__{SUBJECT_FIELD}": subject}).first()


def ask(
    name: str,
    subject: str = "",
    *,
    title: str = "",
    refresh: str = "",
    primary: bool = False,
    compact: bool = False,
) -> Ask:
    """The control that asks for declared work, standing as its last job does.

    Live work is followed. Work that failed says why until it is asked for
    again. Work that ended well stands idle: the page shows what it stored.
    It says the work's declared words, which no caller overrides.
    """

    work = _named(name)
    if not work.offered:
        raise LookupError(f"Outbound work {name!r} is offered to nobody.")
    job = last_job(name, subject)
    standing = standing_of(job_run(job)) if job is not None else Standing()
    if job is not None and not standing.live and standing.state != FAILED:
        standing = Standing()
    reason = "" if standing.live else _refusal(work, subject)
    return Ask(
        work.words,
        reverse("jobs:ask", args=[name]),
        standing=standing,
        status_url=reverse("jobs:status", args=[job.pk]) if job is not None and standing.live else "",
        refresh=refresh,
        name=SUBJECT_FIELD,
        value=subject,
        title=reason or title,
        primary=primary,
        compact=compact,
        disabled=bool(reason),
    )
