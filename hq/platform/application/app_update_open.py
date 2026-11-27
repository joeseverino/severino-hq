"""Open PR: have Renovate open the pull request of an app's newer release.

The press asks a run of the app's one repository (``request_open``). Where
the last run's report says Renovate holds the app's branch, the run names it
as Renovate's ``checkedBranches``, as if its box on the Dependency Dashboard
were ticked; where it holds nothing of it, the run sees the newer release
itself. The run's outcome says what became of the branch and the pull
request it opened, and that pull request comes back through the
repository's reading, when the update's state moves to Approve.

Its security properties: the press is the read ``project.dependency_updates.run``
asks, of one subject, under the same consent and audit; the run's token holds
that one repository.
"""

from dataclasses import dataclass

from django.db import transaction
from django.utils import timezone

lazy from hq.domains.jobs.models import JobState

from .app_update_approval import App, Holder
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .record_errors import RecordNotFound
from .security import Capability, Principal
lazy from .app_updates import UpdateKind, app_branch, of_app, update_state
lazy from .asks import DONE, FAILED, RUNNING, Asked, AskedAnswer, Standing
lazy from .cadence import controller_standing
lazy from .dependency_updates import asked_at, last_run, request_open
lazy from .labels import dotted
lazy from .refusal_status import RefusalEnvelope
lazy from .ui import ended

OPEN = "app_update.open"


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
def open_update(command: OpenUpdateCommand, *, principal: Principal) -> AskedAnswer | RefusalEnvelope:
    """Ask a run of the app's repository that opens the branch Renovate
    holds it under, or that sees its newer release where it holds none."""

    del principal
    states = [update_state(item) for item in of_app(command.repository, command.app)]
    kinds = {state.kind for state in states}
    # Asked again while it is under way, or once Renovate opened it, it is
    # the same request: answered with where it stands, never refused.
    opening = (UpdateKind.HELD, UpdateKind.UNOPENED)
    if under_way := next((state.opening for state in states if state.opening and state.opening.live), None):
        return Asked(under_way, message=ended(f"Already asked. {dotted(under_way.label, under_way.note)}")).answer()
    if not kinds & set(opening):
        if kinds & {UpdateKind.OPEN, UpdateKind.MERGED}:
            return Asked(Standing(DONE, OPENED), message=OPENED).answer()
        raise RecordNotFound("Renovate holds no update of that app.")
    branch = app_branch(command.repository, command.app)
    return request_open(command.repository, branch.branch if branch and branch.held else "", message=ASKED)


def open_standing(repository: str, app: str) -> Standing | None:
    """How the last Open PR of ``app`` stands, as the one fact its card says
    it under: the run asked, running, failed, or ended having opened the
    app's pull request, which its outcome names. None before one is asked,
    and once the run left the app's branch held, when the update's state
    says what Renovate waits for."""

    if asked_at(repository) is not None:
        said = controller_standing().cannot("", now=timezone.now())
        return _opening(f"{RENOVATE} is asked", said)
    run = last_run(repository)
    if run is None:
        return None
    if run.seen == JobState.RUNNING:
        return _opening("Renovate is running")
    if run.seen != JobState.SUCCEEDED:
        return Standing(FAILED, dotted(RENOVATE, run.seen.label, ASKS_AGAIN), word="Failed")
    branch = app_branch(repository, app)
    if branch is None or branch.pull_request is None:
        return None
    return _opening(f"Renovate opened pull request #{branch.pull_request}", "HQ reads it next")


# How an Open PR stands, as its card says it.
OPENING = "Opening"
RENOVATE = "Renovate's run"
ASKS_AGAIN = "Open PR asks again"
ASKED = "Renovate is asked to open its pull request."
OPENED = "Renovate has opened its pull request."


def _opening(*lines: str) -> Standing:
    """An Open PR under way, and the step it is at."""

    return Standing(RUNNING, dotted(*lines), word=OPENING)


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            OPEN,
            "Have Renovate open an app's update: a run of its repository that opens the branch Renovate holds it under.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            OpenUpdateCommand,
            open_update,
            execution_notes=(
                "Refuse an app Renovate neither holds nor has opened a pull request for.",
                "Ask a run of the app's repository alone.",
                "Where Renovate holds its branch, the run opens it, as if its box on the Dependency Dashboard were ticked.",
                "The run's outcome names the pull request it opened, which comes back with the repository's next read.",
            ),
            label="Open PR",
            wants="have Renovate open an app update's pull request",
        ),
    )
