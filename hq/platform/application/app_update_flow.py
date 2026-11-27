"""An app update's way from a newer release to running it, declared as a
sequence (``sequences.SequenceSpec``): the pull request Renovate opens, its
checks on GitHub, the approval that is the person's press, the preview on the
host, the merge on GitHub, the apply on the host and the health check after
it. Each step is read from the one record that holds it: the repository's
reading for the first two, and for the rest the operation each press queued,
found by the correlation an approval's steps share."""

from dataclasses import dataclass

from django.utils.text import capfirst

from hq.domains.control_plane.models import OperationRequest

from .app_updates import Managed, UpdateKind, UpdateState, of_app, update_state
from .asks import DONE, FAILED, IDLE, RUNNING, Standing
from .card_items import Answer
from .containers import containers
from .github_estate import check_step, repositories
from .labels import dotted
from .moments import when
from .projection import read_once
from .sequences import By, Example, Read, SequenceSpec, Step, StepSpec
from .tone import Tone
lazy from .app_update_approval import REFUSED, pull_applied, waits_on
lazy from .automation_manifest import REVISION_INPUT as REVISION
lazy from .automation_runs import CHECK
lazy from .resource_operations import THEN
lazy from .work_runs import run_row, standing_of

# How a step stands, by the tone its answer is said in.
_STATES = {Tone.GOOD: DONE, Tone.ATTENTION: RUNNING, Tone.SERIOUS: FAILED}
# The checks as a whole, by the summary state the read gives them.
_CHECKS = {"success": ("Passed", DONE), "pending": ("Running", RUNNING), "failure": ("Failing", FAILED)}
WAITING = Standing(IDLE, word="Waiting")
# The kinds of update HQ moves through its steps; the rest a person moves by hand.
_MOVED = (UpdateKind.OPEN, UpdateKind.MERGED, UpdateKind.HELD, UpdateKind.UNOPENED)
_ACTIVE = (OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)


@dataclass(frozen=True, slots=True)
class _Asked:
    """The operations of the newest approval of an update at its head, each
    after the one that queued it, or the newest apply of a merged commit."""

    preview: OperationRequest | None = None
    merge: OperationRequest | None = None
    apply: OperationRequest | None = None


def _asked(state: UpdateState) -> _Asked:
    if state.update is not None:
        update = state.update
        return read_once(f"app_update_flow.{update.repository}#{update.number}@{update.head}", lambda: _approved(state))
    if state.kind == UpdateKind.MERGED and (managed := state.managed) is not None:
        head = str((repositories()[managed.repository].head or {}).get("sha") or "")
        return read_once(f"app_update_flow.{managed.repository}@{head}", lambda: _applying(managed, head))
    return _Asked()


def _approved(state: UpdateState) -> _Asked:
    """The newest preview at the update's head, and the newest merge and apply
    its correlation joins since it was asked."""

    update = state.update
    correlation = f"{update.repository}#{update.number}@{update.head[:12]}"
    asked = list(OperationRequest.objects.filter(input__correlation=correlation).order_by("-created_at"))
    preview = next((each for each in asked if each.action == "run" and (each.input or {}).get(CHECK)), None)
    since = [each for each in asked if preview is None or each.created_at >= preview.created_at]
    merge = next((each for each in since if each.action == "merge"), None)
    apply = next((each for each in since if each.action == "run" and CHECK not in (each.input or {})), None)
    return _Asked(preview, merge, apply if merge is not None else None)


def _applying(managed: Managed, head: str) -> _Asked:
    """The newest run that applied ``head``; none once a container of the app
    was read after it ended, since what that container runs says it no longer holds."""

    apply = (
        OperationRequest.objects.filter(action="run", **{f"input__parameters__{REVISION}": head})
        .exclude(input__has_key=CHECK)
        .order_by("-created_at")
        .first()
    )
    if apply is not None and apply.completed_at is not None:
        items = of_app(managed.repository, managed.app)
        if any(item.running.observed_at and item.running.observed_at > apply.completed_at for item in items):
            return _Asked()
    return _Asked(apply=apply)


def _then(operation: OperationRequest | None) -> dict:
    return ((operation.result or {}).get(THEN) or {}) if operation is not None else {}


def _ran(operation: OperationRequest, *, ended: str = "") -> Standing:
    """A step that is a run, as every list of work says it, said ``ended``
    with where it stopped once it ended badly."""

    run = run_row(operation)
    standing = standing_of(run)
    if standing.state != FAILED and not ended:
        return standing
    task, said = run.stopped
    lines = (f"At “{task}”" if task else "", said, *(line for line, _tone in run.evidence.amiss))
    return Standing(FAILED, dotted(*lines), since=standing.since, word=ended or standing.label, url=standing.url)


def _ended_badly(asked: _Asked) -> bool:
    """Whether the newest approval stopped before its merge landed, so the person may approve again."""

    if asked.merge is None:
        return asked.preview is not None and asked.preview.state not in _ACTIVE
    return asked.merge.state == OperationRequest.State.FAILED


def _pull_request(state: UpdateState) -> Standing | Read | None:
    if state.kind not in _MOVED:
        return None
    if state.kind == UpdateKind.MERGED:
        return Standing(DONE, note=f"In {state.managed.repository}", word="Merged")
    if (update := state.update) is not None:
        return Standing(DONE, note=update.title, word=f"{update.label} open", url=update.url)
    return Read(_answered(state.answer), yours=state.action is not None)


def _answered(answer: Answer) -> Standing:
    """A step as an answer says it: its word, its reasons as its sentence."""

    reasons = dotted(*(line for line in answer.because if isinstance(line, str)))
    return Standing(_STATES.get(answer.tone, IDLE), note=reasons, word=answer.short)


def _checks(state: UpdateState) -> Standing | Read | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    if (update := state.update) is None:
        return WAITING
    said = update.checks_said
    word, at = _CHECKS.get(str(update.checks.get("state") or ""), ("None reported", IDLE))
    parts = tuple(map(check_step, update.check_runs))
    standing = Standing(at, note=said.why or ("" if parts else said.badge), word=word)
    return Read(standing, parts)


def _approve(state: UpdateState) -> Standing | Read | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    asked = _asked(state)
    if (asked.preview or asked.merge) is not None and not _ended_badly(asked):
        return Standing(DONE, word="Approved")
    return Read(Standing(IDLE, word="Yours to approve" if state.action else "Waiting"), yours=True)


def _preview(state: UpdateState) -> Standing | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    preview = _asked(state).preview
    if preview is None:
        return WAITING
    if preview.state == OperationRequest.State.SUCCEEDED:
        return Standing(DONE, word="Previewed", url=run_row(preview).url)
    return _ran(preview, ended="" if preview.state in _ACTIVE else "Preview failed")


def _merge(state: UpdateState) -> Standing | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    asked = _asked(state)
    if asked.merge is None:
        refused = str(_then(asked.preview).get("refused") or "")
        return Standing(FAILED, note=refused, word="Not merged") if refused else WAITING
    merge = asked.merge
    if merge.state in _ACTIVE:
        standing = _ran(merge)
        return Standing(standing.state, dotted(standing.note, waits_on(merge)), standing.since, word=standing.label)
    if merge.state == OperationRequest.State.SUCCEEDED:
        return Standing(DONE, word="Merged", url=state.update.url if state.update else "")
    result = merge.result or {}
    word, why = REFUSED.get(str((result.get("status") or {}).get("refused") or "")) or (
        "Merge failed",
        str(result.get("message") or ""),
    )
    return Standing(FAILED, note=why or "GitHub gave no reason", word=word)


def _apply(state: UpdateState) -> Standing | Read | None:
    if state.kind not in _MOVED:
        return None
    asked = _asked(state)
    if asked.apply is None:
        if refused := str(_then(asked.merge).get("refused") or ""):
            return Standing(FAILED, note=refused, word="Not applied")
        if state.kind == UpdateKind.MERGED:
            return Read(Standing(IDLE, word="Yours to apply" if state.action else "Waiting"), yours=True)
        return WAITING
    apply = asked.apply
    if apply.state in _ACTIVE:
        return _ran(apply)
    if apply.state != OperationRequest.State.SUCCEEDED:
        return _ran(apply, ended="Apply failed")
    # Applied only where every machine's pull released every app.
    if not pull_applied(run_row(apply).evidence.stated):
        return _ran(apply, ended="Incomplete")
    return Standing(DONE, word="Applied", url=run_row(apply).url)


def _health(state: UpdateState) -> Standing | Read | None:
    """What each container it moves says of its own health once its apply
    ended well, as HQ last read it; waiting until a read after it ended."""

    if state.kind not in _MOVED:
        return None
    apply = _asked(state).apply
    since = apply.completed_at if apply is not None and apply.state == OperationRequest.State.SUCCEEDED else None
    if since is None:
        return WAITING
    items = state.update.containers if state.update else of_app(state.managed.repository, state.managed.app)
    read = [item for item in items if item.running.observed_at and item.running.observed_at > since]
    if not read or len(read) < len(items):
        return Standing(IDLE, note="Once HQ reads it after the apply", word="Waiting")
    checks = [(item, item.running.check) for item in read if item.running.check is not None]
    if not checks:
        return Standing(DONE, word="None declared")
    parts = tuple(
        Step(
            item.running.name,
            Standing(DONE if check.tone == Tone.GOOD else FAILED, when(item.running.observed_at), word=check.value),
            by=By.HOST,
        )
        for item, check in checks
    )
    worst = next((check for _item, check in checks if check.tone != Tone.GOOD), None)
    if worst is not None:
        return Read(Standing(FAILED, word=capfirst(worst.value)), parts)
    return Read(Standing(DONE, word="Healthy"), parts)


def _about(state: UpdateState) -> str:
    if state.update is not None:
        return f"{state.update.repository}{state.update.label}"
    return f"{state.managed.repository}/{state.managed.app}" if state.managed else ""


def _subject(address: str) -> UpdateState:
    """The update of the container ``address`` names, ``machine:container``."""

    return update_state(next(item for item in containers() if item.address == address))


# The example estate's containers, by the step each seed leaves its update at.
_UPDATED, _MERGED, _HELD = "docker-host-01:home-automation", "docker-host-01:immich", "docker-host-01:jellyfin"

UPDATE = SequenceSpec(
    "app_update",
    (
        StepSpec("Pull request", _pull_request, By.RENOVATE),
        StepSpec("Checks", _checks, By.GITHUB),
        StepSpec("Approve", _approve, By.YOU),
        StepSpec("Preview", _preview, By.HOST),
        StepSpec("Merge", _merge, By.GITHUB),
        StepSpec("Apply", _apply, By.HOST),
        StepSpec("Health check", _health, By.HOST),
    ),
    about=_about,
    subject=_subject,
    examples={
        "held": Example("app-updates", _HELD, "Pull request", IDLE),
        "opening": Example("app-update-ticking", _HELD, "Pull request", RUNNING),
        "not-opened": Example("app-update-not-ticked", _HELD, "Pull request", FAILED),
        "checks-running": Example("app-update-checks-running", _UPDATED, "Checks", RUNNING),
        "to-approve": Example("app-updates", _UPDATED, "Approve", IDLE),
        "previewing": Example("update-queued", _UPDATED, "Preview", "queued"),
        "preview-failed": Example("update-preview-failed", _UPDATED, "Preview", FAILED),
        "applying": Example("update-held", _UPDATED, "Apply", RUNNING),
        "not-applied": Example("update-not-applied", _UPDATED, "Apply", FAILED),
        "applied": Example("update-applied", _UPDATED, "", ""),
        "merged": Example("app-updates", _MERGED, "Apply", IDLE),
    },
)
