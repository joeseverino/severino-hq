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

from dataclasses import dataclass
from typing import Any

from django.db import transaction

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.provider_adapters.github import DASHBOARD_BOX_KIND

from .app_update_approval import App, Holder
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .record_errors import RecordNotFound
from .security import Capability
lazy from .app_updates import UpdateKind, held_branch, of_app, update_state
lazy from .asks import AskedAnswer
lazy from .capabilities import Consented, consent
lazy from .dependency_updates import DependencyRunCommand, request_run
lazy from .github_estate import repositories
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource, suggest_key
lazy from .refusal_status import RefusalEnvelope, refusal_of
lazy from .resource_operations import OperationAsked, acting_for, queue_reconcile

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

    kinds = {update_state(item).kind for item in of_app(command.repository, command.app)}
    if not kinds & {UpdateKind.HELD, UpdateKind.UNOPENED}:
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
