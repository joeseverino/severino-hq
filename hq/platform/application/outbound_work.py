"""Work that reaches outside the process, declared once.

A request never waits on a network, a process or a timer
(``hq.platform.core.outbound``), so work that does is not done by the view or
the capability that asks for it. A domain says what the work is, as an
``OutboundWork``, and everything about running it is derived from that one
declaration:

- the job that runs it off the request, one at a time (``hq.domains.jobs``);
- the capability of the same name, so the API, MCP, the command centre and a
  command line ask for it through the same authorization, consent and denial
  record as any other command, and are answered at once;
- the route its button posts to and the status resource the button follows;
- the control itself (``ask``), standing as the stored job does;
- the audit entries: who asked, how it ended, how long it took, what changed.

The work is a function. It is called on the job's own thread, never inside a
request, with a ``Progress`` to say what it is doing, the subject it was asked
about and the principal that asked.
"""

import inspect
from annotationlib import Format
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from typing import Any

from django.core.exceptions import ImproperlyConfigured
from pydantic import BaseModel, ConfigDict

from .asks import FAILED, Ask, Standing, job_standing
from .contracts import DOTTED_NAME
from .integration_specs import CapabilitySpec
from .security import Principal

# What outbound work may be: a change somewhere else, never a read of HQ.
EFFECTS = frozenset({"remote_write", "destructive", "infrastructure_change"})
# ``jobs.Job.kind`` holds the name.
NAME_LIMIT = 64
# The button's label, and the job's.
LABEL_LIMIT = 60
# The field a control posts its subject in.
SUBJECT_FIELD = "subject"


@dataclass(frozen=True, slots=True)
class OutboundWork:
    """One piece of work a domain does outside the process.

    ``name`` is the job's kind, the capability's name and the last segment of
    the route. ``run`` is called as ``run(progress, subject=, principal=)`` and
    returns what the job stores as its result; it raises ``jobs.Failed`` with a
    sentence to end failed and say why.

    Work about one record names what that record is in ``subject_label``; its
    subject is then required, and is the capability's target. ``refuse``
    answers, from what HQ holds, why the work cannot be asked for a subject
    right now ("" when it can): the control is drawn unusable with that reason
    and an ask is refused with it before any job is recorded.
    """

    name: str
    label: str
    summary: str
    required_capability: str
    run: Callable[..., dict[str, Any] | None]
    subject_label: str = ""
    subject_help: str = ""
    subject_resource: str | None = None
    refuse: Callable[[str], str] | None = None
    effect: str = "remote_write"
    execution_notes: tuple[str, ...] = ()

    @property
    def takes_subject(self) -> bool:
        return bool(self.subject_label)


class _NoArguments(BaseModel):
    """What is acted on is the target; there is nothing else to say."""

    model_config = ConfigDict(extra="forbid")


def validate(work: object) -> OutboundWork:
    """Refuse a declaration HQ could not run, when the composition loads."""

    if not isinstance(work, OutboundWork):
        raise ImproperlyConfigured("Outbound work is declared as OutboundWork.")
    if not DOTTED_NAME.fullmatch(work.name) or len(work.name) > NAME_LIMIT:
        raise ImproperlyConfigured(f"Invalid outbound work name {work.name!r}.")
    if not work.label or work.label != work.label.strip() or len(work.label) > LABEL_LIMIT:
        raise ImproperlyConfigured(f"Outbound work {work.name!r} needs a short label.")
    if work.effect not in EFFECTS:
        raise ImproperlyConfigured(f"Outbound work {work.name!r} has invalid effect {work.effect!r}.")
    if work.refuse is not None and not callable(work.refuse):
        raise ImproperlyConfigured(f"Outbound work {work.name!r} refuse is not callable.")
    try:
        inspect.signature(work.run, annotation_format=Format.STRING).bind(None, subject="", principal=None)
    except TypeError as exc:
        raise ImproperlyConfigured(
            f"Outbound work {work.name!r} must be run(progress, *, subject, principal)."
        ) from exc
    return work


@cache
def _declared() -> dict[str, OutboundWork]:
    from .plugins import plugin_outbound_work

    declared: dict[str, OutboundWork] = {}
    for work in plugin_outbound_work():
        validate(work)
        if work.name in declared:
            raise ImproperlyConfigured(f"Duplicate outbound work {work.name!r}.")
        declared[work.name] = work
    return declared


def declared_work() -> dict[str, OutboundWork]:
    """Every piece of outbound work the composition declares, by name."""

    return _declared()


def clear_outbound_work_cache() -> None:
    _declared.cache_clear()


def capability_for(work: OutboundWork) -> CapabilitySpec:
    """The capability an adapter asks for the work through."""

    validate(work)

    def about(
        command: Any, *, principal: Principal, expected_updated_at: Any = None, current_key: str = ""
    ) -> dict[str, Any]:
        return request_work(work, current_key, principal=principal)

    def whole(command: Any, *, principal: Principal, expected_updated_at: Any = None) -> dict[str, Any]:
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
            "Runs as a background job, one at a time. The answer says it started; the job says how it ended.",
        ),
        label=work.label,
    )


def _refusal(work: OutboundWork, subject: str) -> str:
    return (work.refuse(subject) if work.refuse else "") or ""


def _requester(principal: Principal) -> Any:
    """The signed-in person a web principal stands for, for the job's record."""

    if principal.interface != "web":
        return None
    from django.contrib.auth import get_user_model

    users = get_user_model()
    return users._default_manager.filter(**{users.USERNAME_FIELD: principal.actor}).first()


def request_work(work: OutboundWork, subject: str, *, principal: Principal) -> dict[str, Any]:
    """Ask for the work and answer at once, as a capability answers.

    A caller with a request to answer gets a job on its own thread. A command
    line has no thread that outlives it, so there the work runs to its end
    before the answer.
    """

    from hq.domains.jobs.models import Job
    from hq.domains.jobs.runner import JobConflict, run, start

    principal.require(work.required_capability)
    reason = _refusal(work, subject)
    if reason:
        return {"ok": False, "error": {"code": "precondition", "message": reason}}

    def job_work(progress: Any) -> dict[str, Any]:
        return work.run(progress, subject=subject, principal=principal) or {}

    waited = principal.interface == "cli"
    try:
        job = (run if waited else start)(
            work.name,
            work.label,
            job_work,
            actor=principal.actor,
            requested_by=_requester(principal),
            request={SUBJECT_FIELD: subject},
        )
    except JobConflict:
        live = Job.objects.filter(kind=work.name, state__in=("queued", "running")).first()
        same = live is not None and live.request.get(SUBJECT_FIELD, "") == subject
        return {
            "ok": True,
            "started": False,
            **({"job": str(live.pk)} if live is not None and same else {}),
            "message": f"{work.label} is already running.",
        }
    if not waited:
        return {
            "ok": True,
            "started": True,
            "job": str(job.pk),
            "message": f"{job.label} started; the job reports how it ended.",
        }
    standing = job_standing(job)
    if standing.state == FAILED:
        return {"ok": False, "error": {"code": "operation_failed", "message": standing.note}}
    return {"ok": True, "started": True, "job": str(job.pk), "message": standing.note, "result": job.result}


def run_now(name: str, subject: str = "", *, principal: Principal) -> Any:
    """Do declared work to its end on the calling thread, and return its job.

    For a command a timer runs: the same job row, one-at-a-time rule and audit
    entry as a pressed button. Refused inside a request.
    """

    from hq.domains.jobs.runner import run

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


def last_job(name: str, subject: str = "") -> Any:
    """The job that last did this work for this subject, or None. One query."""

    from hq.domains.jobs.models import Job
    from hq.domains.jobs.runner import reap

    job = Job.objects.filter(kind=name, **{f"request__{SUBJECT_FIELD}": subject}).first()
    if job is not None and job.is_stale:
        reap(name)
        job.refresh_from_db()
    return job


def ask(
    name: str,
    subject: str = "",
    *,
    label: str = "",
    title: str = "",
    refresh: str = "",
    primary: bool = False,
    compact: bool = False,
) -> Ask:
    """The control that asks for declared work, standing as its last job does.

    Live work is followed. Work that failed says why until it is asked for
    again. Work that ended well stands idle: the page shows what it stored.
    """

    from django.urls import reverse

    work = _named(name)
    job = last_job(name, subject)
    standing = job_standing(job) if job is not None else Standing()
    if job is not None and not standing.live and standing.state != FAILED:
        standing = Standing()
    reason = "" if standing.live else _refusal(work, subject)
    return Ask(
        label or work.label,
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
