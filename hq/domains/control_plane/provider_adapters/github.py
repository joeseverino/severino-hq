"""GitHub as HQ declares it: continuous delivery, the pull requests it merges and the Renovate boxes it ticks.

Delivery: the controller (controller/providers/github_delivery.go) compares
the running image's lock with each extension's latest admission on GitHub and
reports each stage there; HQ starts nothing. A pull request is merged at the
head commit a person approved (controller/providers/github_merge.go), and a
Dependency Dashboard box is ticked so Renovate opens a branch it holds
(controller/providers/github_dashboard.go).
"""

from collections.abc import Mapping
from typing import Annotated, Any, Literal, get_args

from pydantic import Field

from hq.platform.application.describe import Describe
from hq.platform.application.record_fields import RecordId
from hq.platform.application.ui import ended

from ..connection_shapes import GITHUB_APP
from ..observations import github as readings
from ..observations.github import PROVIDER
from ..permissions import GitHub
from ..provider_spec import ConnectionKind, InboundSpec, ProviderModel, ProviderSpec, SharedValue, applies, locked

KIND = "github.delivery"
# Shared values (``SHARED``): the controller reports a sweep's record with the
# same defaults, which is what lets `adopts` recognize it.
CURRENT = "Every extension's latest admission, confirmed on GitHub"
COMPOSE_WORKFLOW = ".github/workflows/compose.yml"
MAIN_BRANCH = "main"
# A repository as owner/name, and a commit by its whole sha, as GitHub names each.
REPOSITORY = r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"
COMMIT = r"^[0-9a-f]{40}$"


def _from_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository": record["repository"],
        "workflow": record["workflow"],
        "branch": record["branch"],
        "production": record["production"],
    }


GOAL = "Production runs the latest approved commit of each plugin."


def _plugin_state(item: Mapping[str, Any]) -> str:
    """One plugin's line: what runs, and what stands between it and the latest."""

    running, approved = str(item.get("running", ""))[:7], str(item.get("admitted", ""))[:7]
    if not approved or approved == running:
        return f"Live at {running}" if running else "Live"
    stage = str(item.get("stage", "")).strip()
    return " ".join(ended(said) for said in (f"Runs {running}, latest approved is {approved}", stage) if said)


def _readout(_spec: dict[str, Any], status: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    rows = [("Goal", GOAL, "")]
    rows.extend(
        (str(item.get("plugin", "")), "", _plugin_state(item))
        for item in status.get("extensions") or ()
        if isinstance(item, Mapping)
    )
    return tuple(rows)


def _console(record: dict[str, Any]) -> str:
    """The run a plugin is waiting on, else the repository's runs."""

    for item in record.get("extensions") or ():
        if isinstance(item, Mapping) and item.get("run_url"):
            return str(item["run_url"])
    repository = str(record.get("repository", ""))
    return f"https://github.com/{repository}/actions" if repository else ""


class GitHubDeliverySpec(ProviderModel):
    """The repository and workflow that deploy HQ, which HQ checks production against."""

    repository: str = Field(
        pattern=REPOSITORY,
        title="Repository",
        description="The repository whose deploy workflow deploys HQ.",
    )
    workflow: Annotated[str, Describe("", api="The workflow file, in the repository, that deploys HQ.")] = Field(
        default=COMPOSE_WORKFLOW,
        pattern=r"^\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$",
        title="Deploy workflow",
    )
    branch: Annotated[str, Describe("", api="The branch the workflow deploys from.")] = Field(
        default=MAIN_BRANCH, pattern=r"^[A-Za-z0-9_./-]+$", title="Branch"
    )
    production: Annotated[Literal[CURRENT], Describe("", api="What production is held to.")] = Field(  # type: ignore[valid-type]  # the shared constant, which mypy reads as a value
        default=CURRENT, title="Production"
    )


DEFINITION = ProviderSpec(
    KIND,
    "Checks that production runs each plugin's latest approved commit, and reports it on its pull request.",
    GitHubDeliverySpec,
    actions={"reconcile": applies(automatic=True)},
    label="HQ deploys",
    label_plural="HQ deploys",
    name=lambda _spec: "HQ deploys",
    reported_fields=("production",),
    console=_console,
    console_label="Open deploy on GitHub",
    connection_providers=(PROVIDER,),
    _from_record=_from_record,
    identity=lambda spec: (spec["repository"],),
    key_hint=lambda _record: "delivery",
    adopts=lambda record: record.get("production") == CURRENT,
    sample_record={
        "repository": "example/host",
        "workflow": COMPOSE_WORKFLOW,
        "branch": MAIN_BRANCH,
        "production": CURRENT,
        "extensions": [],
    },
    readout=_readout,
    advanced_fields=("workflow", "branch", "production"),
    declaration_only=True,
    removal_note=lambda spec: (
        "HQ stops reporting on plugin commits. Their approvals still "
        f"start the deploy in {ended(spec.get('repository', 'the repository'))}"
    ),
)


PULL_REQUEST_KIND = "github.pull_request"
# A repository a declaration names.
type Repository = Annotated[
    str, Describe("", api="The repository, as owner/name."), Field(pattern=REPOSITORY, title="Repository")
]
# The action that merges one, ``OperationRequest.Action.MERGE``.
MERGE = "merge"
# Why GitHub would not merge one: the head moved after the approval (409),
# GitHub refused it as it stands (405), its head's checks fail or have not all
# passed yet, or the App is not installed on its repository.
MergeRefusal = Literal["head_moved", "not_mergeable", "checks_failing", "checks_pending", "not_installed"]


class GitHubPullRequestSpec(ProviderModel):
    """One pull request, at the head commit a person approved merging."""

    repository: Repository
    number: Annotated[RecordId, Describe("", api="The pull request's number.")] = Field(title="Number")
    head_sha: Annotated[str, Describe("", api="The head commit approved; GitHub refuses the merge once it moves.")] = (
        Field(pattern=COMMIT, title="Head commit")
    )


class GitHubMergeStatus(ProviderModel):
    """What a merge reports: the commit it made, or why GitHub refused it."""

    merge_sha: str = ""
    refused: MergeRefusal | None = None


PULL_REQUEST = ProviderSpec(
    PULL_REQUEST_KIND,
    "A pull request HQ merges, at the head commit you approved.",
    GitHubPullRequestSpec,
    actions={
        "reconcile": locked("Nothing to apply. Merge it from its update."),
        MERGE: applies(),
    },
    label="Pull request",
    name=lambda spec: f"{spec.get('repository', '')}#{spec.get('number', '')}",
    connection_providers=(PROVIDER,),
    identity=lambda spec: (str(spec["repository"]), str(spec["number"])),
    key_hint=lambda spec: f"{spec.get('repository', '')}-{spec.get('number', '')}".replace("/", "-"),
    console=lambda spec: f"https://github.com/{spec.get('repository', '')}/pull/{spec.get('number', '')}",
    console_label="Open pull request on GitHub",
    declaration_only=True,
    unobserved_reason="Each merge reports the commit it made.",
)
DASHBOARD_BOX_KIND = "github.dashboard_box"
# The words Renovate marks a box with for a branch it holds with no pull
# request (its dependency-dashboard.ts): ticked, Renovate opens it.
HeldMarker = Literal["unpend", "approve", "unschedule", "unlimit"]


class GitHubDashboardBoxSpec(ProviderModel):
    """One box on a repository's Dependency Dashboard, ticked so Renovate opens its branch's pull request."""

    repository: Repository
    branch: Annotated[str, Describe("", api="The branch Renovate holds.")] = Field(
        pattern=r"^[A-Za-z0-9_./-]+$", max_length=200, title="Branch"
    )
    marker: Annotated[HeldMarker, Describe("", api="The word Renovate marks its box with.")] = Field(title="Marker")


DASHBOARD_BOX = ProviderSpec(
    DASHBOARD_BOX_KIND,
    "A box HQ ticks on a Dependency Dashboard, so Renovate opens the branch's pull request.",
    GitHubDashboardBoxSpec,
    actions={"reconcile": applies()},
    label="Dashboard box",
    name=lambda spec: f"{spec.get('repository', '')} {spec.get('branch', '')}",
    connection_providers=(PROVIDER,),
    identity=lambda spec: (str(spec["repository"]), str(spec["branch"])),
    key_hint=lambda spec: f"{spec.get('repository', '')}-{spec.get('branch', '')}".replace("/", "-"),
    declaration_only=True,
    unobserved_reason="Renovate rewrites the Dashboard on every run.",
)
DEFINITIONS = (DEFINITION, PULL_REQUEST, DASHBOARD_BOX)
OBSERVATIONS = readings.OBSERVATIONS

SHARED = (
    SharedValue(
        "GitHubDeliveryProduction",
        (CURRENT,),
        "What a github.delivery record says of production once every extension's "
        "latest admission runs there. Until then it says what stands between.",
        varnames=("GitHubDeliveryProductionCurrent",),
    ),
    SharedValue(
        "GitHubDeliverySpec",
        GitHubDeliverySpec,
        "A github.delivery declaration. The defaults are the declaration a sweep reports and HQ adopts.",
        refs=(("production", "GitHubDeliveryProduction"),),
    ),
    SharedValue(
        "GitHubPullRequestSpec", GitHubPullRequestSpec, "A github.pull_request declaration: what a merge merges."
    ),
    SharedValue(
        "GitHubDashboardBoxSpec",
        GitHubDashboardBoxSpec,
        "A github.dashboard_box declaration: the box a reconcile ticks.",
    ),
    SharedValue(
        "GitHubMergeRefusal",
        get_args(MergeRefusal),
        "Why GitHub refused a merge, as a refused merge's status says it.",
    ),
    SharedValue(
        "GitHubMergeStatus",
        GitHubMergeStatus,
        "What a github.pull_request merge reports: the commit it made, or why GitHub refused it.",
        mode="validation",
        refs=(("refused", "GitHubMergeRefusal"),),
    ),
)

# The connection this provider's credential arrives through, beside its kinds:
# admitting the module admits both.
CONNECTIONS = {
    # An app's permissions are fine-grained and each token HQ mints is
    # narrowed again to one call's repositories and permissions.
    PROVIDER: ConnectionKind(
        "GitHub App",
        "scoped",
        GITHUB_APP,
        create=(
            "A GitHub App on your account; mise run wire-github-app records its ID, stores its key and points"
            " its webhook at the inbound-events Worker."
        ),
        create_url="https://github.com/settings/apps/new",
        gives="Repo rules on your repositories, and HQ's deploys waiting for your approval.",
    ),
}

# What one App webhook sends from every installation, each event making the
# repositories' reading due, and the webhook's own, since a delivery that
# arrived is one GitHub's log no longer counts as failing; GitHub's docs name
# the permission each needs
# (https://docs.github.com/en/webhooks/webhook-events-and-payloads). A
# deployment review is requested the moment a run starts waiting on a reviewer.
INBOUND = {
    PROVIDER: InboundSpec(
        dict.fromkeys(
            ("push", "workflow_run", "check_suite", "pull_request", "deployment_status", "deployment_review"),
            (readings.REPOSITORY_KIND, readings.WEBHOOK_KIND),
        ),
        path="/github",
        scheme="github-hmac-sha256",
        signature_header="X-Hub-Signature-256",
        delivery_header="X-GitHub-Delivery",
        event_header="X-GitHub-Event",
        action_pointer="/action",
        subject_pointer="/repository/full_name",
        secret_binding="GITHUB_WEBHOOK_SECRET",  # noqa: S106 - the Worker secret's name, never its value
        # "Payloads are capped at 25 MB" (docs.github.com, webhook events and payloads).
        max_body_bytes=25 * 1024 * 1024,
        # The delivery log is not: a delivery that failed never arrives.
        complete=(readings.REPOSITORY_KIND,),
        requires=(
            GitHub.CONTENTS_READ,
            GitHub.ACTIONS_READ,
            GitHub.CHECKS_READ,
            GitHub.PULL_REQUESTS_READ,
            GitHub.DEPLOYMENTS_READ,
        ),
    ),
}
