"""An app update's way from a newer release to running it, as its card
lists it in order: the pull request Renovate opens, its checks, the
approval that merges it, and the apply on its host. Each step is done,
under way, failed or waiting, said from what HQ stores of it, so the one
press the card offers is the next step's."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.utils.text import capfirst

from .app_updates import AppUpdate, UpdateKind, UpdateState, of_app
from .card_items import Answer
from .entity_links import EntityLink, declared_link, entity_link, web_url
from .moments import duration, moment, when
from .references import Kind
from .tone import Tone


@dataclass(frozen=True, slots=True)
class FlowStep:
    """One step of the way, under its title: where it stands, a word over
    its lines, and its title as a link to the page that holds it."""

    title: str
    answer: Answer
    done: bool = False
    link: EntityLink | None = None


# A check's state in a word, by its conclusion once it completed, else its status.
CONCLUDED = {"success": "passed", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed out"}
STATUS = {"queued": "queued", "in_progress": "running", "waiting": "waiting", "pending": "queued"}
# The checks as a whole, by the summary state the read gives them.
CHECKS = {
    "success": ("Passed", Tone.GOOD),
    "pending": ("Running", Tone.ATTENTION),
    "failure": ("Failing", Tone.SERIOUS),
}


def update_flow(state: UpdateState) -> tuple[FlowStep, ...]:
    """Every step of ``state``'s update in order, each as it stands."""

    merged = state.kind == UpdateKind.MERGED
    # An approval whose merge landed says how its apply stands, before the pull request is read closed.
    landed = state.kind == UpdateKind.OPEN and state.beside is not None and state.beside.merged
    opened = merged or state.update is not None
    return (
        _opened(state) if opened else FlowStep("Pull request", state.answer),
        _checks(state.update) if state.update else _waiting("Checks", "Once it opens", done="Done" if merged else ""),
        _decided("Approve", state)
        if state.kind == UpdateKind.OPEN and not landed
        else _waiting("Approve", "Once its checks pass", done="Merged" if merged or landed else ""),
        _decided("Apply", state) if merged or landed else _waiting("Apply", "Once it merges"),
        _health(state) if merged or landed else _waiting("Health check", "Once it applies"),
    )


def _waiting(title: str, until: str, *, done: str = "") -> FlowStep:
    """A step before the one under way, waiting ``until``; or behind it, ``done`` saying how it ended."""

    return FlowStep(title, Answer(done) if done else Answer("Waiting", (until,)), done=bool(done))


def _opened(state: UpdateState) -> FlowStep:
    update = state.update
    if update is None:
        return FlowStep(
            "Pull request", Answer("Merged", (f"In {state.managed.repository}",) if state.managed else ()), done=True
        )
    return FlowStep(
        "Pull request",
        Answer(f"{update.label} open", (update.title,)),
        done=True,
        link=declared_link("Pull request", web_url(update.url), kind="dependency"),
    )


def _checks(update: AppUpdate) -> FlowStep:
    said = update.checks_said
    word, tone = CHECKS.get(str(update.checks.get("state") or ""), ("None reported", Tone.NEUTRAL))
    lines = tuple(_check(each) for each in update.check_runs) or (said.badge,)
    return FlowStep("Checks", Answer(word, (*lines, said.why), tone), done=tone == Tone.GOOD)


def _check(run: Mapping[str, Any]) -> EntityLink | str:
    """One check as its line says it, ``Lint passed 58 s`` or ``Test running``,
    linked to its run on GitHub where the reading names it."""

    status = str(run.get("status") or "")
    conclusion = str(run.get("conclusion") or "")
    word = CONCLUDED.get(conclusion, conclusion) if status == "completed" else STATUS.get(status, status)
    started, ended = moment(run.get("started_at")), moment(run.get("completed_at"))
    said = " ".join(part for part in (str(run.get("name") or ""), word, _took(started, ended)) if part)
    url = web_url(run.get("url"))
    return declared_link(said, url, kind="dependency") if url else said


def _took(started: datetime | None, ended: datetime | None) -> str:
    if started is None:
        return ""
    return str(duration(ended - started, short=True)) if ended is not None else f"since {when(started)}"


def _decided(title: str, state: UpdateState) -> FlowStep:
    """The step whose press the card offers: where it stands once asked,
    else the person's to take, else waiting on what the card says beside it."""

    if (standing := state.beside) is not None:
        run = standing.run
        link = entity_link(Kind.RUN, run.id, label=title) if run is not None and run.url else None
        return FlowStep(title, standing.answer, done=standing.tone == Tone.GOOD, link=link)
    return FlowStep(title, Answer(f"Yours to {title.lower()}") if state.action else Answer("Waiting"))


def _health(state: UpdateState) -> FlowStep:
    """What each container it moves says of its own health once its apply
    was asked, as HQ last read it; waiting until a read after the ask."""

    standing = state.beside
    operation = standing.operation if standing is not None else None
    since = operation.claimed_at or operation.created_at if operation is not None else None
    if since is None or not (standing and standing.taken and standing.tone != Tone.SERIOUS):
        return _waiting("Health check", "Once it applies")
    items = state.update.containers if state.update else of_app(state.managed.repository, state.managed.app)
    read = [item for item in items if item.running.observed_at and item.running.observed_at > since]
    if not read or len(read) < len(items):
        return _waiting("Health check", "Once HQ reads it after the apply")
    checks = [(item, item.running.check) for item in read]
    if all(check is None for _item, check in checks):
        return FlowStep("Health check", Answer("None declared"), done=True)
    worst = next((pair for pair in checks if pair[1] is not None and pair[1].tone != Tone.GOOD), None)
    lines = tuple(
        f"{item.running.name} {check.value} {when(item.running.observed_at)}" for item, check in checks if check
    )
    if worst is not None:
        return FlowStep("Health check", Answer(capfirst(worst[1].value), lines, worst[1].tone))
    return FlowStep("Health check", Answer("Healthy", lines, Tone.GOOD), done=True)
