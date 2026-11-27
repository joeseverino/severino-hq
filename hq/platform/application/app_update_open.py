"""Open PR: have Renovate open the pull request of an app's newer release.

Where Renovate holds the app's branch on the Dependency Dashboard, HQ ticks
its box (``github.dashboard_box``, controller/providers/github_dashboard.go)
and, once the tick lands, asks a dependency run (``succeeded``), so the run
reads the ticked box. Where Renovate holds nothing of it, the run is asked at
once. Either way the pull request comes back through the repository's
reading, and the update's state moves to Approve.

Its security properties: the tick is a reconcile of a declaration, under the
same consent and audit as any other, and its token holds Issues write on that
one repository; the run is the one ``project.dependency_updates.run`` asks.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import transaction
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.observations.github import PULL_REQUEST_KIND
from hq.domains.control_plane.provider_adapters.github import DASHBOARD_BOX_KIND
from hq.domains.jobs.models import JobState

from .app_update_approval import App, Holder, waits_on
from .app_updates import MergeStanding
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .record_errors import RecordNotFound
from .security import Capability
from .tone import Tone
lazy from .app_updates import UpdateKind, held_branch, of_app, update_state
lazy from .asks import DONE, Asked, AskedAnswer, Standing
lazy from .cadence import controller_standing
lazy from .capabilities import Consented, consent
lazy from .dependency_updates import DependencyRunCommand, asked_at, last_run, request_run
lazy from .facts import kinds_read
lazy from .github_estate import repositories
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource, suggest_key
lazy from .refusal_status import RefusalEnvelope, refusal_of
lazy from .resource_operations import THEN, OperationAsked, acting_for, queue_reconcile

OPEN = "app_update.open"
# What a tick's success carries: the run to ask once the box is ticked.
RUN = {"run": True}


@dataclass(frozen=True, slots=True)
class OpenUpdateCommand:
    """The app whose update Renovate is to open."""

    repository: Holder
    app: App


def open_target(repository: str, app: str) -> dict[str, str]:
    """The route arguments a press opening ``app``'s update posts to."""

    owner, _, name = repository.partition("/")
    return {"owner": owner, "name": name, "app": app}


@transaction.atomic
def open_update(command: OpenUpdateCommand, *, consented: Consented) -> OperationAsked | AskedAnswer | RefusalEnvelope:
    """Tick the box Renovate holds the app's branch under, asking a run once
    it lands; with no box, ask the run now."""

    states = [update_state(item) for item in of_app(command.repository, command.app)]
    kinds = {state.kind for state in states}
    # Asked again while it is under way, or once Renovate opened it, it is
    # the same request: answered with where it stands, never refused.
    opening = (UpdateKind.HELD, UpdateKind.UNOPENED)
    if under_way := next((state.standing for state in states if state.at_work and state.kind in opening), None):
        # Renovate's run, asked of the controller, answers as asking it did.
        return under_way.asked() if under_way.operation else under_way.again.answer()
    if not kinds & set(opening):
        if kinds & {UpdateKind.OPEN, UpdateKind.MERGED}:
            return Asked(Standing(DONE, OPENED), message=OPENED).answer()
        raise RecordNotFound("Renovate holds no update of that app.")
    held = held_branch(repositories()[command.repository], command.app)
    if held is None:
        return request_run(DependencyRunCommand(), principal=consent(consented).principal)
    spec = {"repository": command.repository, "branch": str(held["branch"]), "marker": str(held["marker"])}
    current = (
        ManagedResource.objects.select_for_update()
        .filter(kind=DASHBOARD_BOX_KIND, spec__repository=spec["repository"], spec__branch=spec["branch"])
        .first()
    )
    key = current.key if current else suggest_key(DASHBOARD_BOX_KIND, spec)
    save_managed_resource(
        ManagedResourceCommand(key=key, kind=DASHBOARD_BOX_KIND, spec=spec),
        consented=consented,
        current_key=key if current else None,
    )
    box = ManagedResource.objects.select_for_update().get(key=key)
    return queue_reconcile(box, consented=consented, reason="", then=RUN)


_ACTIVE = (OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED)


def open_standing(repository: str, held: Mapping[str, Any] | None) -> MergeStanding | None:
    """How the last Open PR of an app ``repository`` holds stands, as the
    one fact its card says it under: the tick of the branch Renovate ``held``
    it under, then the run that reads it, each with what comes next. None
    before one is asked, and once the pull requests were read after the run
    ended, when the one it opened is the update's next state."""

    since = None
    if held is not None:
        tick = (
            OperationRequest.objects.filter(
                action=OperationRequest.Action.RECONCILE,
                resource__kind=DASHBOARD_BOX_KIND,
                resource__spec__repository=repository,
                resource__spec__branch=str(held.get("branch", "")),
            )
            .order_by("-created_at")
            .first()
        )
        if tick is None:
            return None
        if tick.state in _ACTIVE:
            return _opening(TICKING, waits_on(tick), operation=tick)
        result = tick.result or {}
        if tick.state != OperationRequest.State.SUCCEEDED:
            message = str(result.get("message") or "")
            return MergeStanding("Not ticked", Tone.SERIOUS, lines=(BOX, message, TRIES_AGAIN), operation=tick)
        if refused := str((result.get(THEN) or {}).get("refused") or ""):
            return MergeStanding("Renovate not asked", Tone.SERIOUS, lines=(refused, ASKS_AGAIN), operation=tick)
        since = tick.created_at
    return _run_standing(repository, since)


# How an Open PR stands, as its card says it.
OPENING = "Opening"
BOX = "Its box on the Dependency Dashboard"
TICKING = "Ticking its box on the Dependency Dashboard"
RENOVATE = "Renovate's run"
TRIES_AGAIN = "Open PR tries again"
ASKS_AGAIN = "Open PR asks again"
OPENED = "Renovate has opened its pull request."


def _opening(*lines: str, operation: OperationRequest | None = None) -> MergeStanding:
    """An Open PR under way, and the step it is at."""

    return MergeStanding(OPENING, Tone.ATTENTION, taken=True, lines=lines, operation=operation)


def _run_standing(repository: str, since: datetime | None) -> MergeStanding | None:
    """How the Renovate run asked since ``since`` (any, given none) stands
    for ``repository``: waiting for its hand-off, running, failed, or ended
    before HQ read the pull requests it may have opened."""

    if (asked := asked_at()) is not None and (since is None or asked >= since):
        said = controller_standing().cannot("", now=timezone.now())
        return _opening(f"{RENOVATE} is asked", said)
    run = last_run(repository)
    if run is None or (since is not None and run.started_at < since):
        return None
    if run.seen == JobState.RUNNING:
        return _opening("Renovate is running")
    if run.seen == JobState.SUCCEEDED:
        read = kinds_read().get(PULL_REQUEST_KIND)
        if run.finished_at is not None and (read is None or read < run.finished_at):
            return _opening(f"{RENOVATE} ended", "HQ reads its pull request next")
        return None
    if since is None:
        return None
    return MergeStanding("Failed", Tone.SERIOUS, lines=(RENOVATE, run.seen.label, ASKS_AGAIN))


def ticked(operation: OperationRequest, then: dict[str, Any]) -> dict[str, str]:
    """Ask the run a ticked box carries, as its requester."""

    if operation.resource.kind != DASHBOARD_BOX_KIND or then != RUN:
        return {}
    answer = request_run(DependencyRunCommand(), principal=acting_for(operation))
    return {"run": "asked"} if answer["ok"] else {"refused": refusal_of(answer).message}


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            OPEN,
            "Have Renovate open an app's update: tick its branch's box on the Dependency Dashboard, then ask a run.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            OpenUpdateCommand,
            open_update,
            execution_notes=(
                "Refuse an app Renovate neither holds nor has opened a pull request for.",
                "Where Renovate holds its branch, tick that branch's box; once the tick lands, ask a run.",
                "Where Renovate holds nothing of it, ask a run now.",
                "The pull request comes back with the repository's next read.",
            ),
            label="Open PR",
            wants="have Renovate open an app update's pull request",
        ),
    )
