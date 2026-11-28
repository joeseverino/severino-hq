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
from hq.domains.control_plane.observations.github import PROVIDER as GITHUB
from hq.domains.control_plane.observations.host import PROVIDER as HOST

from .app_update_approval import apply_merged, run_passed
from .app_updates import Managed, UpdateKind, UpdateState, of_app, update_state
from .asks import DONE, FAILED, IDLE, QUEUED, RUNNING, Standing
from .containers import containers
from .github_estate import check_state, check_step, repositories
from .labels import dotted, lower_first
from .moments import when
from .projection import read_once
from .routes import reverse
from .sequences import PERSON, Example, Read, SequenceSpec, Step, StepSpec
from .tone import Tone
from .ui import ended
lazy from .app_update_approval import REFUSED, pull_applied, waits_on
lazy from .automation_manifest import REVISION_INPUT as REVISION
lazy from .automation_runs import CHECK
lazy from .resource_operations import THEN, OperationAsked, operation_asked
lazy from .work_runs import run_row, standing_of

# A step waiting on the steps before it, said in the words its declaration gives.
WAITING = Read(Standing(IDLE))
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


def _pull_request(state: UpdateState) -> Read | None:
    if state.kind not in _MOVED:
        return None
    if state.kind == UpdateKind.MERGED:
        return Read(Standing(DONE, note=f"In {state.managed.repository}", word="Merged"))
    if (update := state.update) is not None:
        return Read(Standing(DONE, word=f"{update.label} opened", url=update.url))
    return Read(state.pull or Standing(IDLE), yours=True)


def _checks(state: UpdateState) -> Read | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    if (update := state.update) is None:
        return WAITING
    said = str(update.checks.get("state") or "")
    word, at = check_state(said) if said else ("None reported", IDLE)
    parts = tuple(map(check_step, update.check_runs))
    passed = f"{sum(part.done for part in parts)} of {len(parts)} passed" if parts else ""
    return Read(Standing(at, note=passed or update.checks_said.why, word=word), parts)


def _approve(state: UpdateState) -> Read | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    asked = _asked(state)
    if (asked.preview or asked.merge) is not None and not _ended_badly(asked):
        return Read(Standing(DONE, word="Approved"))
    if state.adopt:
        return Read(
            Standing(IDLE, state.reason, word="Needs a playbook", url=reverse("control_plane:playbooks")), yours=True
        )
    if state.kind == UpdateKind.OPEN and state.offered:
        return Read(Standing(IDLE, state.said, word="Yours now"), yours=True)
    return Read(Standing(IDLE), yours=True)


def _preview(state: UpdateState) -> Read | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    preview = _asked(state).preview
    if preview is None:
        return WAITING
    if preview.state == OperationRequest.State.SUCCEEDED:
        return Read(Standing(DONE, word="Passed", url=run_row(preview).url))
    return Read(_ran(preview, ended="" if preview.state in _ACTIVE else "Failed"))


def _merge(state: UpdateState) -> Read | None:
    if state.kind == UpdateKind.MERGED or state.kind not in _MOVED:
        return None
    asked = _asked(state)
    if asked.merge is None:
        refused = str(_then(asked.preview).get("refused") or "")
        return Read(Standing(FAILED, note=refused, word="Refused")) if refused else WAITING
    merge = asked.merge
    if merge.state in _ACTIVE:
        standing = _ran(merge)
        return Read(
            Standing(standing.state, dotted(standing.note, waits_on(merge)), standing.since, word=standing.label)
        )
    if merge.state == OperationRequest.State.SUCCEEDED:
        return Read(Standing(DONE, word="Done", url=state.update.url if state.update else ""))
    result = merge.result or {}
    word, why = REFUSED.get(str((result.get("status") or {}).get("refused") or "")) or (
        "Failed",
        str(result.get("message") or ""),
    )
    return Read(Standing(FAILED, note=why or "GitHub gave no reason", word=word))


def _apply(state: UpdateState) -> Read | None:
    if state.kind not in _MOVED:
        return None
    asked = _asked(state)
    if asked.apply is None:
        if refused := str(_then(asked.merge).get("refused") or ""):
            return Read(Standing(FAILED, note=refused, word="Refused"))
        if state.kind == UpdateKind.MERGED:
            return Read(Standing(IDLE, state.said, word="Yours now" if state.offered else "Waiting"), yours=True)
        return WAITING
    apply = asked.apply
    if apply.state in _ACTIVE:
        return Read(_ran(apply))
    if apply.state != OperationRequest.State.SUCCEEDED:
        return Read(_ran(apply, ended="Failed"), yours=state.offered is not None)
    # Applied only where every machine's pull released every app.
    if not pull_applied(run_row(apply).evidence.stated):
        return Read(_ran(apply, ended="Incomplete"), yours=state.offered is not None)
    return Read(Standing(DONE, word="Done", url=run_row(apply).url))


def _health(state: UpdateState) -> Read | None:
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
        return Read(Standing(IDLE, word="Once HQ reads it after the apply"))
    checks = [(item, item.running.check) for item in read if item.running.check is not None]
    if not checks:
        return Read(Standing(DONE, word="None declared"))
    parts = tuple(
        Step(
            item.running.name,
            Standing(DONE if check.tone == Tone.GOOD else FAILED, when(item.running.observed_at), word=check.value),
            by=HOST,
        )
        for item, check in checks
    )
    worst = next((check for _item, check in checks if check.tone != Tone.GOOD), None)
    if worst is not None:
        return Read(Standing(FAILED, word=capfirst(worst.value)), parts)
    return Read(Standing(DONE, word="Healthy"), parts)


def again(state: UpdateState) -> OperationAsked | None:
    """What a second press answers while HQ moves the update, or once it
    moved it: where it stands, queuing nothing; None while a press would act."""

    asked = _asked(state)
    if not state.at_work or (operation := asked.apply or asked.merge or asked.preview) is None:
        return None
    step = state.flow.at or state.flow.steps[-1]
    said = f"{step.title} {lower_first(step.line)}"
    message = ended(f"Already asked. {said}" if step.live else said)
    return operation_asked(operation, queued=False, standing=step.standing, message=message)


def _about(state: UpdateState) -> str:
    return f"Update {state.move}" if state.move else "Update"


def _subject(address: str) -> UpdateState:
    """The update of the container ``address`` names, ``machine:container``."""

    return update_state(next(item for item in containers() if item.address == address))


# The example estate's containers, by the step each seed leaves its update at.
_UPDATED, _MERGED, _HELD = "docker-host-01:home-automation", "docker-host-01:immich", "docker-host-01:jellyfin"

PULL_REQUEST = StepSpec("Pull request", _pull_request, GITHUB)
CHECKS = StepSpec("Checks", _checks, GITHUB)
APPROVE = StepSpec("Approve", _approve, PERSON, waiting="Yours, once checks pass")
PREVIEW = StepSpec("Preview", _preview, HOST, waiting="After you approve")
MERGE = StepSpec("Merge", _merge, GITHUB, waiting="After the preview")
APPLY = StepSpec("Apply", _apply, HOST, waiting="After it merges")
HEALTH = StepSpec("Health check", _health, HOST, waiting="After it applies")

UPDATE = SequenceSpec(
    "app_update",
    (PULL_REQUEST, CHECKS, APPROVE, PREVIEW, MERGE, APPLY, HEALTH),
    about=_about,
    subject=_subject,
    follow_ons={OperationRequest.Action.RUN: run_passed, OperationRequest.Action.MERGE: apply_merged},
    examples={
        "held": Example("app-updates", _HELD, PULL_REQUEST, IDLE),
        "opening": Example("app-update-running", _HELD, PULL_REQUEST, RUNNING),
        "not-opened": Example("app-update-failed", _HELD, PULL_REQUEST, FAILED),
        "checks-running": Example("app-update-checks-running", _UPDATED, CHECKS, RUNNING),
        "to-approve": Example("app-updates", _UPDATED, APPROVE, IDLE),
        "previewing": Example("update-queued", _UPDATED, PREVIEW, QUEUED),
        "preview-failed": Example("update-preview-failed", _UPDATED, PREVIEW, FAILED),
        "applying": Example("update-held", _UPDATED, APPLY, RUNNING),
        "not-applied": Example("update-not-applied", _UPDATED, APPLY, FAILED),
        "applied": Example("update-applied", _UPDATED),
        "merged": Example("app-updates", _MERGED, APPLY, IDLE),
    },
)


def sequences() -> tuple[SequenceSpec, ...]:
    """The workflows the containers domain declares."""

    return (UPDATE,)
