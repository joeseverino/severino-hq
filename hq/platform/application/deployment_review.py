"""Approve or reject a deploy waiting on HQ, a repository's custom deployment protection rule.

A press writes the ``github.deployment_review`` declaration for the run at
the commit the person saw and queues its reconcile, which the controller
answers on GitHub (controller/providers/github_review.go).

Its security properties: HQ never answers on its own, since an answer is
only a person's press or an agent's held request a person approved
(``waits_for_a_person``). The run is checked against the repository's
reading here and read again by the controller before it answers, so a run
that moved, stopped waiting or never waited is refused, and an event from
GitHub only makes the reading due. The controller's token holds Deployments
write on that one repository. A commit production already runs is never
approved.
"""

from dataclasses import dataclass
from typing import Annotated, Any

from django.db import transaction
from pydantic import Field

from hq.domains.control_plane.models import ManagedResource
from hq.domains.control_plane.provider_adapters.github import COMMIT, DEPLOYMENT_REVIEW_KIND, REPOSITORY, ReviewState

from .command_fields import Describe
from .contracts import Effect
from .integration_specs import CapabilitySpec
from .record_errors import PolicyError, RecordNotFound
from .record_fields import RecordId
from .security import Capability
lazy from .approvals import ApprovalSubject, every_call, measured_by, waits_for_a_person
lazy from .capabilities import Consented
lazy from .github_estate import repositories
lazy from .infrastructure import ManagedResourceCommand, save_managed_resource, suggest_key
lazy from .resource_operations import OperationAsked, queue_reconcile

APPROVE = "deployment.approve"
REJECT = "deployment.reject"
# An environment as GitHub names one, and as a route segment carries it.
ENVIRONMENT = r"^[^/\s][^/]{0,254}$"


@dataclass(frozen=True, slots=True)
class ReviewDeploymentCommand:
    """The run waiting to deploy, at the commit you saw."""

    repository: Annotated[
        str, Field(pattern=REPOSITORY, max_length=140), Describe("The repository it runs in, as owner/name.")
    ]
    run_id: Annotated[RecordId, Describe("The workflow run that waits.")]
    environment: Annotated[str, Field(pattern=ENVIRONMENT), Describe("The environment it waits to deploy to.")]
    head_sha: Annotated[
        str,
        Field(pattern=COMMIT),
        Describe("The commit you saw it deploy.", api="The run's `sha`; a run at another commit is refused."),
    ]


def waiting_run(repository: str, run_id: int, environment: str) -> dict[str, Any] | None:
    """The run of ``repository``'s reading that waits on ``environment``, or None."""

    repo = repositories().get(repository)
    return next(
        (
            run
            for run in (repo.waiting if repo else ()) or ()
            if run.get("id") == run_id and environment in (run.get("environments") or ())
        ),
        None,
    )


def hq_rules(repository: str, environment: str) -> bool:
    """Whether HQ's App is an enabled custom deployment protection rule on ``repository``'s ``environment``."""

    repo = repositories().get(repository)
    return any(
        item.get("name") == environment and item.get("hq_rule") for item in (repo.environments if repo else ()) or ()
    )


def production_runs(repository: str, sha: str) -> bool:
    """Whether ``sha`` is the commit of ``repository``'s newest production deploy."""

    repo = repositories().get(repository)
    return bool(sha) and repo is not None and (repo.production or {}).get("sha") == sha


def review_target(repository: str, run: dict[str, Any], environment: str) -> dict[str, str]:
    """The route arguments a press answering ``run``'s wait on ``environment`` posts to."""

    owner, _, name = repository.partition("/")
    return {
        "owner": owner,
        "name": name,
        "run_id": str(run.get("id", "")),
        "environment": environment,
        "head_sha": str(run.get("sha", "")),
    }


def _measured(command: ReviewDeploymentCommand) -> ApprovalSubject:
    """What a held answer is of: the run's wait at its commit, so a run that moved or stopped waiting stales it."""

    run = waiting_run(command.repository, command.run_id, command.environment)
    return ApprovalSubject(
        DEPLOYMENT_REVIEW_KIND,
        f"{command.repository}#{command.run_id}:{command.environment}",
        {"head_sha": str((run or {}).get("sha", ""))},
    )


def _answer(state: ReviewState):
    @transaction.atomic
    def review(command: ReviewDeploymentCommand, *, consented: Consented) -> OperationAsked:
        run = waiting_run(command.repository, command.run_id, command.environment)
        if run is None:
            raise RecordNotFound(f"No run of {command.repository} waits to deploy to {command.environment}.")
        if not hq_rules(command.repository, command.environment):
            raise PolicyError(f"HQ is not a protection rule on {command.environment}. Answer the run on GitHub.")
        if run.get("sha") != command.head_sha:
            raise PolicyError("The run deploys another commit since you saw it. Review it again.")
        if state == "approved" and production_runs(command.repository, command.head_sha):
            raise PolicyError(f"Production already runs {command.head_sha[:7]}. Reject it instead.")
        spec = {
            "repository": command.repository,
            "run_id": command.run_id,
            "environment": command.environment,
            "head_sha": command.head_sha,
            "state": state,
        }
        current = (
            ManagedResource.objects.select_for_update()
            .filter(
                kind=DEPLOYMENT_REVIEW_KIND,
                spec__repository=command.repository,
                spec__run_id=command.run_id,
                spec__environment=command.environment,
            )
            .first()
        )
        key = current.key if current else suggest_key(DEPLOYMENT_REVIEW_KIND, spec)
        save_managed_resource(
            ManagedResourceCommand(key=key, kind=DEPLOYMENT_REVIEW_KIND, spec=spec),
            consented=consented,
            current_key=key if current else None,
        )
        return queue_reconcile(ManagedResource.objects.select_for_update().get(key=key), consented=consented, reason="")

    return waits_for_a_person(every_call)(measured_by(_measured)(review))


# Each refusal both answers share, as their execution notes say them.
_CHECKS = (
    "Refuse a run the repository's reading does not hold waiting on that environment, or at another commit.",
    "Refuse an environment where HQ's GitHub App is not an enabled protection rule.",
    "The controller reads the run again and answers only while it still waits there at that commit.",
    "When an agent asks, it waits for an operator's approval.",
)


def capabilities() -> tuple[CapabilitySpec, ...]:
    return (
        CapabilitySpec(
            APPROVE,
            "Approve a deploy waiting on HQ's deployment protection rule, at the commit you saw.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            ReviewDeploymentCommand,
            _answer("approved"),
            execution_notes=("Refuse a commit production already runs.", *_CHECKS),
            label="Approve deploy",
            verb="Approve",
            wants="approve a deploy",
        ),
        CapabilitySpec(
            REJECT,
            "Reject a deploy waiting on HQ's deployment protection rule, at the commit you saw.",
            Effect.INFRASTRUCTURE_CHANGE,
            Capability.MANAGE_INFRASTRUCTURE,
            ReviewDeploymentCommand,
            _answer("rejected"),
            execution_notes=_CHECKS,
            label="Reject deploy",
            verb="Reject",
            wants="reject a deploy",
        ),
    )
