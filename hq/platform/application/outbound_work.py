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
from functools import cache
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, ConfigDict
lazy from django.utils import timezone

lazy from hq.domains.jobs.models import Job
lazy from hq.domains.jobs.runner import ALREADY_RUNNING, JobConflict, Progress, due as is_due, run, start

from .asks import FAILED, Ask, Standing, job_standing
from .contracts import DOTTED_NAME
from .integration_specs import CapabilitySpec, requested
from .projection import read_once
from .record_errors import RecordNotFound
from .security import Principal, person_of
lazy from .domains import host_specs
lazy from .plugins import plugin_outbound_work
lazy from .routes import reverse

# What outbound work may be: a change somewhere else, never a read of HQ.
EFFECTS = frozenset({"remote_write", "destructive", "infrastructure_change"})
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
    ``every`` so the host's ``due`` timer keeps an extension's data current.
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
    effect: str = "remote_write"
    execution_notes: tuple[str, ...] = ()
    # For work about no one record: once this long has passed since it last
    # succeeded, the host's due timer does it.
    every: timedelta | None = None
    # What its button says where that is shorter than the label ("Refresh").
    verb: str = ""

    @property
    def takes_subject(self) -> bool:
        return bool(self.subject_label)

    @property
    def words(self) -> str:
        """What its button says."""

        return self.verb or self.label


class _NoArguments(BaseModel):
    """What is acted on is the target; there is nothing else to say."""

    model_config = ConfigDict(extra="forbid")


def validate(work: object) -> OutboundWork:
    """Refuse a declaration HQ could not run, when the composition loads."""

    if not isinstance(work, OutboundWork):
        raise ImproperlyConfigured("Outbound work is declared as OutboundWork.")
    if not DOTTED_NAME.fullmatch(work.name) or len(work.name) > NAME_LIMIT:
        raise ImproperlyConfigured(f"Invalid outbound work name {work.name!r}.")
    if not work.label or any(text != text.strip() or len(text) > LABEL_LIMIT for text in (work.label, work.verb)):
        raise ImproperlyConfigured(f"Outbound work {work.name!r} needs a short label.")
    if work.effect not in EFFECTS:
        raise ImproperlyConfigured(f"Outbound work {work.name!r} has invalid effect {work.effect!r}.")
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


@cache
def _declared() -> dict[str, OutboundWork]:
    declared: dict[str, OutboundWork] = {}
    # The host's domains declare theirs as an extension does, so neither can
    # take a name the other holds.
    for work in (*host_specs("outbound"), *plugin_outbound_work()):
        validate(work)
        if work.name in declared:
            raise ImproperlyConfigured(f"Duplicate outbound work {work.name!r}.")
        declared[work.name] = work
    return declared


def declared_work() -> dict[str, OutboundWork]:
    """Every piece of outbound work the composition declares, by name."""

    return _declared()


def capability_specs() -> tuple[CapabilitySpec, ...]:
    """The capability each piece of declared work is asked for through."""

    return tuple(capability_for(work) for work in declared_work().values())


def clear_outbound_work_cache() -> None:
    _declared.cache_clear()


def capability_for(work: OutboundWork) -> CapabilitySpec:
    """The capability an adapter asks for the work through."""

    validate(work)

    def about(_command: Any, *, principal: Principal, current_key: str = "") -> dict[str, Any]:
        return request_work(work, current_key, principal=principal)

    def whole(_command: Any, *, principal: Principal) -> dict[str, Any]:
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


def request_work(work: OutboundWork, subject: str, *, principal: Principal) -> dict[str, Any]:
    """Ask for the work and answer at once, as a capability answers.

    A caller with a request to answer gets a job on its own thread. A command
    line has no thread that outlives it, so there the work runs to its end
    before the answer. Reached only as a capability, which has authorized the
    caller.
    """

    reason = _refusal(work, subject)
    if reason:
        return {"ok": False, "error": {"code": "precondition", "message": reason}}

    def job_work(progress: Progress) -> dict[str, Any]:
        return work.run(progress, subject=subject, principal=principal) or {}

    waited = principal.interface == "cli"
    try:
        job = (run if waited else start)(
            work.name,
            work.label,
            job_work,
            actor=principal.actor,
            requested_by=person_of(principal),
            request={SUBJECT_FIELD: subject},
        )
    except JobConflict:
        live = Job.objects.filter(kind=work.name, state__in=("queued", "running")).first()
        same = live is not None and live.request.get(SUBJECT_FIELD, "") == subject
        return {
            "ok": True,
            "started": False,
            **({"job": str(live.pk)} if live is not None and same else {}),
            "message": ALREADY_RUNNING if same else ANOTHER_RUNNING,
        }
    if not waited:
        return {"ok": True, "started": True, "job": str(job.pk), "message": requested(work.words, subject)}
    standing = job_standing(job)
    if standing.state == FAILED:
        return {"ok": False, "error": {"code": "operation_failed", "message": standing.note}}
    return {"ok": True, "started": True, "job": str(job.pk), "message": standing.note, "result": job.result}


def run_now(name: str, subject: str = "", *, principal: Principal) -> Job:
    """Do declared work to its end on the calling thread, and return its job.

    For a command a timer runs: the same job row, one-at-a-time rule and audit
    entry as a pressed button. Refused inside a request.
    """

    work = _named(name)
    principal.require(work.required_capability)
    reason = _refusal(work, subject)
    if reason:
        raise ValueError(reason)

    def job_work(progress: Any) -> dict[str, Any]:
        return work.run(progress, subject=subject, principal=principal) or {}

    return run(
        work.name,
        work.label,
        job_work,
        actor=principal.actor,
        request={SUBJECT_FIELD: subject},
    )


def _named(name: str) -> OutboundWork:
    work = declared_work().get(name)
    if work is None:
        raise LookupError(f"No outbound work named {name!r} is declared.")
    return work


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
    job = last_job(name, subject)
    standing = job_standing(job) if job is not None else Standing()
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
