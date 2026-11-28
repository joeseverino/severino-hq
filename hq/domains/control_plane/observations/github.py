"""Readings of GitHub: each repository the App's installation covers, the
public profile of each account a sign-in names, and how each dependency update
run ended. What a repository's record holds is docs/Derived-Facts.md#github.

Which repositories is the installation's own list, so choosing them on GitHub
is the configuration. Secrets and variables' values are never read, so the
schema has nowhere to put them; of a log, only a failing step's last lines
are (``JobFailure``).
"""

from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import Field, StrictInt
from pydantic.json_schema import SkipJsonSchema

lazy from hq.platform.application import dependency_updates
lazy from hq.platform.application.moments import moment, since_when, when
lazy from hq.platform.application.ui import counted

from ..permissions import GitHub
from .contract import ObservationRecord, ObservationSpec, ReadingPart, Stamp

PROVIDER = "github_app"
REPOSITORY_KIND = "github.repository"
# Read in the same pass as each repository, and carried with it.
WORKFLOW_RUN_KIND = "github.workflow_run"
PULL_REQUEST_KIND = "github.open_pull_request"
REPOSITORY_KINDS: tuple[str, ...] = (REPOSITORY_KIND, WORKFLOW_RUN_KIND, PULL_REQUEST_KIND)
# How often each repository is read in full when GitHub's change query sees
# nothing new: the bound on how stale what the query cannot see gets (alerts,
# variables, runners, environments, branch rules, access, artifacts).
FULL_READ_EVERY = timedelta(hours=1)

# Refusable one at a time: a private repository on a free plan has no code
# scanning, and that should not hide its pull requests.
CODE_SCANNING = ReadingPart("code_scanning", "Code scanning alerts", requires=(GitHub.CODE_SCANNING_ALERTS_READ,))
DEPENDABOT = ReadingPart("dependabot", "Dependabot alerts", requires=(GitHub.DEPENDABOT_ALERTS_READ,))
# GitHub calls this secret scanning, and its permission keeps GitHub's name. The
# part is named for what it finds, leaked credentials, because this vocabulary
# reaches the topology, which carries no such word.
LEAKED_CREDENTIALS = ReadingPart(
    "leaked_credentials", "Leaked credential alerts", requires=(GitHub.SECRET_SCANNING_ALERTS_READ,)
)
BRANCH_RULES = ReadingPart("branch_rules", "Branch rules", requires=(GitHub.ADMINISTRATION_READ,))
ENVIRONMENTS = ReadingPart("environments", "Environments", requires=(GitHub.ADMINISTRATION_READ,))
RUNNERS = ReadingPart("runners", "Self-hosted runners", requires=(GitHub.ADMINISTRATION_READ,))
IMAGES = ReadingPart("images", "Container images", requires=(GitHub.PACKAGES_READ,))
ACCESS = ReadingPart("access", "Access and Actions policy", requires=(GitHub.ADMINISTRATION_READ,))
VARIABLES = ReadingPart("variables", "Actions variables", requires=(GitHub.VARIABLES_READ,))
# Each workflow's ``uses:`` lines and the commit each tag names now: what a
# pinning fix replaces them with.
WORKFLOW_PINS = ReadingPart("workflow_pins", "Workflow action pins", requires=(GitHub.CONTENTS_READ,))
# Of a repository opted in to updates: each app's Compose files, the open
# Dependency Dashboard, and the images each update moves.
COMPOSE = ReadingPart("compose", "Compose apps", requires=(GitHub.CONTENTS_READ, GitHub.ISSUES_READ))
# A failing job's step log, refused one job at a time while the checks read.
JOB_LOGS = ReadingPart("job_logs", "Failed job logs", requires=(GitHub.ACTIONS_READ,))
# Of each open pull request, what GitHub's GraphQL answers in one query per
# repository: its merge state, its review decision and every check on its head,
# commit statuses among them.
PULL_STATES = ReadingPart(
    "pull_states",
    "Merge, review and check states",
    requires=(GitHub.PULL_REQUESTS_READ, GitHub.CHECKS_READ, GitHub.COMMIT_STATUSES_READ),
)
PARTS = (
    CODE_SCANNING,
    DEPENDABOT,
    LEAKED_CREDENTIALS,
    BRANCH_RULES,
    ENVIRONMENTS,
    RUNNERS,
    IMAGES,
    ACCESS,
    VARIABLES,
    WORKFLOW_PINS,
    COMPOSE,
    JOB_LOGS,
)
# The most Compose files a record keeps, which the controller reads from the
# record's schema in the bridge contract.
COMPOSE_KEPT = 500
# The most failing jobs a commit's checks say why of: the causes a failing
# finding shows, and so the logs the controller reads, from the record's schema.
FAILURES_KEPT = 4


# The parts a record counts open alerts of, by their part names.
AlertPart = Literal["code_scanning", "dependabot", "leaked_credentials"]
# One part's open alerts by severity, as the controller lowercases it. A count
# is a number, never its text: every reader sums them.
AlertCounts = dict[str, Annotated[StrictInt, Field(ge=0)]]


class Collaborator(ObservationRecord):
    login: str
    role: str = ""


class DeployKey(ObservationRecord):
    title: str = ""
    read_only: bool = False
    last_used: Stamp = None
    created_at: Stamp = None


# A feature's status, as GitHub's security_and_analysis answers it; None where
# GitHub answered no status.
FeatureStatus = Literal["enabled", "disabled"] | None


class Security(ObservationRecord):
    """Each feature of GitHub's ``security_and_analysis`` HQ reads, by
    GitHub's own name; one the plan does not offer is absent.

    Declared here so the bridge's door takes the names as the contract's own,
    though several hold the word "secret": each holds only a status.
    """

    advanced_security: FeatureStatus = None
    code_security: FeatureStatus = None
    dependabot_security_updates: FeatureStatus = None
    secret_scanning: FeatureStatus = None
    secret_scanning_push_protection: FeatureStatus = None
    secret_scanning_non_provider_patterns: FeatureStatus = None
    secret_scanning_validity_checks: FeatureStatus = None
    secret_scanning_ai_detection: FeatureStatus = None
    secret_scanning_delegated_alert_dismissal: FeatureStatus = None
    secret_scanning_delegated_bypass: FeatureStatus = None


class RepositoryAccess(ObservationRecord):
    """Who and what can reach a repository, and how its Actions may run."""

    visibility: str = ""
    collaborators: tuple[Collaborator, ...] = ()
    deploy_keys: tuple[DeployKey, ...] = ()
    allowed_actions: str = ""
    pinning_required: bool = False
    # The workflow token's default permission, GitHub's word for it: a name
    # the door would strike, holding only one of two words.
    token: Literal["read", "write"]
    token_approves_reviews: bool = False
    vulnerability_alerts: bool = False
    # None where the plan offers none of the features, which is not "off".
    security: Security | None = None


class PullFile(ObservationRecord):
    """One file a pull request changes."""

    path: str = ""
    status: str = ""
    additions: int = 0
    deletions: int = 0
    # The app whose directory, ``compose/<app>/``, holds it at any depth, or
    # "" for a file outside every app's.
    app: str = ""


class JobFailure(ObservationRecord):
    """Why one failing check failed: for a workflow's job, the step it failed
    at, the error GitHub's log of that step marks, and the step's last lines
    of output before it; for any other check, no step, its output's title as
    the error and its summary as the log. Each is the check's own words,
    bounded (``github_failures.go``, ``github_checks.go``)."""

    step: str = ""
    error: str = ""
    log: list[str] = []


class Checks(ObservationRecord):
    """A commit's checks, each standing as its last run did, and why each
    failing job of a workflow run failed, by the job's name."""

    state: str = ""
    total: int = 0
    failing: list[str] = []
    running: int = 0
    names: list[str] = []
    failures: dict[str, JobFailure] = Field(default={}, max_length=FAILURES_KEPT)


class CheckRun(ObservationRecord):
    """One check as its last run stands: a workflow run's job, another app's
    check, or a commit status, in GitHub's words, lower-cased, with when it
    started and finished and its page where GitHub says them."""

    name: str = ""
    status: str = ""
    conclusion: str = ""
    started_at: Stamp = None
    completed_at: Stamp = None
    url: str = ""


class PullRequestRecord(ObservationRecord):
    """One open pull request. ``check_runs`` is every check on its head, and
    ``mergeable_state`` and ``review_state`` GitHub's own words for whether it
    can merge and where its review stands (``github_pulls.go``). For one
    Renovate opened (its branch under ``renovate/``), ``checks`` is the head's
    with why each failing job failed, ``files`` each file it changes, one page
    of them, and ``changed_files`` how many GitHub says it changes; they are
    None and 0 for any other. ``apps`` is
    ``{app, read, images: [{service, before, after}]}`` per app whose
    directory it changes, each service whose image differs at its head, in a
    repository opted in to updates, and None otherwise (``github_compose.go``).
    """

    connection_ref: str
    repository: str
    number: int = 0
    title: str = ""
    url: str = ""
    # The login that opened it.
    author: str = ""
    draft: bool = False
    created_at: Stamp = None
    updated_at: Stamp = None
    # The head and base commits, and the branches they are the tips of.
    head: str = ""
    base: str = ""
    branch: str = ""
    base_branch: str = ""
    labels: list[str] = []
    # The repository its head branch lives in, owner/name: another
    # repository's for a fork, "" once that fork is gone.
    head_repository: str = ""
    # GitHub's merge state (``clean``, ``blocked``, ``behind``, ``dirty``,
    # ``unstable``, ``has_hooks``, ``unknown``) and review decision
    # (``approved``, ``changes_requested``, ``review_required``, or "" where
    # none is asked for).
    mergeable_state: str = ""
    review_state: str = ""
    check_runs: list[CheckRun] = []
    checks: Checks | None = None
    files: list[PullFile] | None = None
    changed_files: int = 0
    apps: list[dict[str, Any]] | None = None
    # What the update run's artifact problem comment says it could not
    # update, read while its checks fail.
    artifact_problem: list[str] = []
    # Sent in place of a repository's records when the change query found
    # it unchanged (``carry_held``).
    carried: bool = False


class WorkflowRunRecord(ObservationRecord):
    """A workflow's newest run on one branch, under the workflow's name now,
    and each of its jobs. On the default branch a pull request's run is not
    the branch's; on any other the newest run of either is."""

    connection_ref: str
    repository: str
    branch: str = ""
    name: str = ""
    id: int = 0
    event: str = ""
    sha: str = ""
    status: str = ""
    conclusion: str = ""
    url: str = ""
    created_at: Stamp = None
    jobs: list[CheckRun] = []
    carried: bool = False


class RepositoryRecord(ObservationRecord):
    connection_ref: str
    repository: str
    private: bool = False
    # Read-only on GitHub: nothing more can land in it to protect.
    archived: bool = False
    url: str = ""
    default_branch: str = ""
    pushed_at: Stamp = None
    head: dict[str, Any] = {}
    checks: Checks = Checks()
    # The check names the newest pull request runs.
    pull_request_checks: list[str] = []
    waiting: list[dict[str, Any]] = []
    release: dict[str, Any] | None = None
    deployments: list[dict[str, Any]] = []
    # Open alerts counted by part and severity: ``{"dependabot": {"high": 2}}``.
    # A part that was refused is absent, never zero.
    alerts: dict[AlertPart, AlertCounts] = {}
    # ``{number, package, ecosystem, manifest_path, severity,
    # first_patched_version, url}`` per open Dependabot alert;
    # ``first_patched_version`` is None where no version fixes it yet. None
    # when the Dependabot part was refused.
    dependabot_alerts: list[dict[str, Any]] | None = None
    artifacts: list[dict[str, Any]] = []
    rules: dict[str, Any] | None = None
    environments: list[dict[str, Any]] = []
    runners: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    access: RepositoryAccess | None = None
    # Names only: a variable's value is never read.
    variables: list[str] | None = None
    # ``{path, uses, action, ref, sha}`` per ``uses:`` line not pinned to a
    # commit, in the workflows and the local composite actions; ``sha`` is the
    # commit its ref names, or "" where it could not be read.
    pins: list[dict[str, Any]] | None = None
    # Each ``uses:`` of a reusable workflow another repository holds, pinned or
    # not: what it uses in turn is not read here.
    called_workflows: list[str] | None = None
    # Each app's Compose files on the default branch, ``compose/<app>/<file>``,
    # of a repository opted in to updates: empty for any other, or where the
    # Compose part was refused.
    compose: list[str] = Field(default=[], max_length=COMPOSE_KEPT)
    # The login HQ's dependency update run opens its pull requests as, the
    # App's bot, read with the Compose part.
    updater: str = ""
    # ``{path, service, image}`` per service of those files as compose-go
    # loads them at the default branch's head (``head``): what a merge pins.
    compose_images: list[dict[str, Any]] = []
    # ``{app, commits}`` per app with a service built on its host: each commit
    # from the newest that changed ``compose/<app>/`` to the head, newest
    # first, the revisions that build what a merge does.
    builds: list[dict[str, Any]] = []
    # What GitHub's change query answered when this record was read, hashed,
    # and when it was read in full: the plan HQ sends the controller.
    fingerprint: str = ""
    read_at: Stamp = None
    # Sent in place of a record the change query found unchanged: the record
    # HQ holds stands (``carry_held``), and none is stored without one.
    carried: bool = False


def carry_held(last: Iterable[Mapping[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records, each carried one replaced by every record HQ holds of the
    same repository, and by none where HQ holds none."""

    held: dict[str, list[dict[str, Any]]] = {}
    for record in last:
        held.setdefault(str(record.get("repository", "")), []).append(dict(record))
    kept: list[dict[str, Any]] = []
    for record in records:
        if record.get("carried"):
            kept.extend(held.get(str(record.get("repository", "")), ()))
        else:
            kept.append(record)
    return kept


# What GitHub shows anyone, so no credential reads it: the provider is GitHub
# itself, not a connection.
PUBLIC_PROVIDER = "github"
PROFILE_KIND = "github.profile"
# GitHub rations anonymous calls by the hour, and a profile changes slowly, so
# it is read this often and when somebody asks.
_PROFILE_EVERY = timedelta(hours=6)
AVATAR = ReadingPart("avatar", "Profile picture")
# The bounds of a profile record, which the controller reads from the record's
# schema in the bridge contract. GitHub allows an address 60 anonymous calls an
# hour, and one account costs the profile, the stars and the picture, then a
# release call and an advisory call for each watched repository:
# 3 + 2 x WATCHED_KEPT.
WATCHED_KEPT = 15
AVATAR_LENGTH = 90000


class ProfileRecord(ObservationRecord):
    login: str
    name: str = ""
    bio: str = ""
    url: str = ""
    followers: int = 0
    following: int = 0
    public_repos: int = 0
    created_at: Stamp = None
    company: str = ""
    location: str = ""
    website: str = ""
    social: str = ""
    public_gists: int = 0
    hireable: bool = False
    # A ``data:`` URI: a page loads no image from another host.
    avatar: str = Field(default="", max_length=AVATAR_LENGTH)
    # How many repositories the account stars, of which the newest are watched.
    starred: int = 0
    # ``{name, url, description, language, stars, starred_at, release, advisories}``.
    watched: list[dict[str, Any]] = Field(default=[], max_length=WATCHED_KEPT)


DEPENDENCY_RUN_KIND = "github.dependency_run"
# Opted-in repositories are updated once a day and when somebody asks.
_DEPENDENCY_RUN_EVERY = timedelta(days=1)
# An account and a repository as a run names them: GitHub's rules, lower-cased.
# The controller reads both from this record's schema in the bridge contract.
OWNER = r"^[a-z0-9-]{1,39}$"
REPOSITORY = r"^([a-z0-9-]{1,39})/[a-z0-9._-]{1,100}$"
# A branch Renovate names, as the hand-off spells one.
BRANCH = r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,199}$"


class Branch(ObservationRecord):
    """One branch as the run's report says Renovate left it
    (``controller/host/renovate.go`` readReport)."""

    repository: str = Field(pattern=REPOSITORY)
    branch: str = Field(pattern=BRANCH)
    # Renovate's word for what it did with the branch (its ``BranchResult``):
    # ``pr-created``, ``needs-approval``, ``pending``, ``not-scheduled``…
    result: str = Field(pattern=r"^[a-z][a-z-]{0,39}$")
    # The number of the pull request the branch has, None for none.
    pull_request: int | None = None
    # The package files its upgrades change, as the repository names them.
    files: list[str] = Field(default=[], max_length=20)


class DependencyRunRecord(ObservationRecord):
    """One account's update run: what root reported of it, never its log."""

    owner: str = Field(pattern=OWNER)
    repositories: list[Annotated[str, Field(pattern=REPOSITORY)]] = []
    # How the run ended, as root reports it, or ``handed_off``: the controller
    # gave root a token for it and no result has come back yet.
    outcome: str = Field(pattern=r"^(ok|failed|timeout|handed_off)$")
    # None until the run has finished.
    exit_code: int | None
    started_at: Stamp = None
    finished_at: Stamp = None
    # Each branch of its repositories its report names, once it has finished.
    branches: list[Branch] = Field(default=[], max_length=500)


ALLOWANCE_KIND = "github.allowance"
# Others' spend within one hour that is past what the deploy pipeline and the
# dependency update run take of it, a few requests a deploy and some hundreds
# a day: more than that is something else holding the App's key or a token.
OTHERS_SPENT_ATTENTION = 1000


class AllowanceRecord(ObservationRecord):
    """One App installation's hourly REST allowance as a sweep began, and what the sweep spent of it."""

    connection_ref: str
    installation: int
    account: str = ""
    limit: int
    # What GitHub had counted, and had left, before the sweep's first request.
    used: int
    remaining: int
    resets_at: Stamp = None
    started_at: Stamp = None
    # The requests the sweep spent, by GitHub's count of each answer.
    spent: int
    # What everything else spent between the first sweep of this hour HQ can
    # judge and this one: HQ's, set as it stores the reading
    # (with_others_spent), so no controller sends it. None where no earlier
    # sweep of the same hour is held.
    others_spent: SkipJsonSchema[int | None] = None
    others_since: SkipJsonSchema[str | None] = None


WEBHOOK_KIND = "github.webhook"


class Delivery(ObservationRecord):
    """One delivery of the App's webhook, as GitHub's delivery log says it."""

    event: str
    action: str = ""
    # GitHub's words for the answer ("OK", "failed to connect") and its
    # status code, 0 where nothing answered.
    status: str = ""
    status_code: int = 0
    delivered_at: Stamp = None


class WebhookRecord(ObservationRecord):
    """What the App's webhook sends and how its newest deliveries went: the
    events the App is subscribed to, from ``GET /app``, and its delivery log's
    newest page, from ``GET /app/hook/deliveries``."""

    connection_ref: str
    app: str
    # The account that owns the App, and whether it is an organization: the
    # two the App's settings page is addressed by.
    owner: str
    organization: bool = False
    events: list[str] = []
    # The deliveries read, the newest of them, and how many of the newest
    # failed in a row: 0 once one succeeds. A success is an answer from 200
    # to 399, as the log's own status filter counts it (``github_webhook.go``).
    deliveries: int = 0
    newest: Delivery | None = None
    failing: int = 0


def app_settings(record: Mapping[str, Any]) -> str:
    """The App's settings page on GitHub, where its events are ticked."""

    app, owner = str(record.get("app", "")), str(record.get("owner", ""))
    if not app:
        return ""
    if record.get("organization") and owner:
        return f"https://github.com/organizations/{owner}/settings/apps/{app}"
    return f"https://github.com/settings/apps/{app}"


def _installation(record: Mapping[str, Any]) -> tuple[str, str]:
    return str(record.get("connection_ref", "")), str(record.get("installation", ""))


def _others(before: Mapping[str, Any] | None, record: Mapping[str, Any]) -> tuple[int | None, str | None]:
    """Others' spend this hour up to ``record``, and since when: the requests
    GitHub counted between two sweeps beyond the first one's own. A pair across
    a reset is in two hours and is not judged."""

    if before is None or not record.get("resets_at") or before.get("resets_at") != record.get("resets_at"):
        return None, None
    then, now = moment(before.get("started_at")), moment(record.get("started_at"))
    if then is None or now is None or then >= now:
        return None, None
    between = int(record.get("used") or 0) - int(before.get("used") or 0) - int(before.get("spent") or 0)
    return max(between, 0) + int(before.get("others_spent") or 0), str(
        before.get("others_since") or before["started_at"]
    )


def with_others_spent(last: Iterable[Mapping[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each allowance record with what others spent of its hour, from the
    same installation's record in the reading last stored."""

    earlier = {_installation(record): record for record in last}
    for record in records:
        record["others_spent"], record["others_since"] = _others(earlier.get(_installation(record)), record)
    return records


def _allowance(record: Mapping[str, Any]) -> str:
    """The hour as one line: "4,210 of 5,000 left, resets 2:00 PM; HQ spent 12, others 30"."""

    said = f"{int(record.get('remaining') or 0):,} of {int(record.get('limit') or 0):,} left"
    if (resets := moment(record.get("resets_at"))) is not None:
        said += f", resets {when(resets)}"
    said += f"; HQ spent {int(record.get('spent') or 0):,}"
    if (others := record.get("others_spent")) is not None:
        said += f", others {int(others):,}"
        if (since := moment(record.get("others_since"))) is not None:
            said += f" {since_when(since)}"
    return said


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        REPOSITORY_KIND,
        PROVIDER,
        "GitHub repository",
        RepositoryRecord,
        requires=(
            GitHub.METADATA_READ,
            GitHub.CONTENTS_READ,
            GitHub.CHECKS_READ,
            GitHub.ISSUES_READ,
            GitHub.PULL_REQUESTS_READ,
            GitHub.ACTIONS_READ,
            GitHub.DEPLOYMENTS_READ,
            GitHub.CODE_SCANNING_ALERTS_READ,
            GitHub.DEPENDABOT_ALERTS_READ,
            GitHub.SECRET_SCANNING_ALERTS_READ,
            GitHub.ADMINISTRATION_READ,
            GitHub.PACKAGES_READ,
            GitHub.VARIABLES_READ,
        ),
        parts=PARTS,
        title=lambda record: str(record.get("repository", "")),
        relation="Built from repository",
        console=lambda record: str(record.get("url", "")),
        from_last=carry_held,
    ),
    ObservationSpec(
        WORKFLOW_RUN_KIND,
        PROVIDER,
        "GitHub workflow run",
        WorkflowRunRecord,
        requires=(GitHub.METADATA_READ, GitHub.ACTIONS_READ, GitHub.CHECKS_READ),
        title=lambda record: f"{record.get('repository', '')} {record.get('branch', '')}: {record.get('name', '')}",
        read_with=REPOSITORY_KIND,
        describe=lambda record: str(record.get("conclusion") or record.get("status") or ""),
        relation="Workflow run of",
        console=lambda record: str(record.get("url", "")),
        at=lambda record: str(record.get("created_at") or ""),
        from_last=carry_held,
    ),
    ObservationSpec(
        PULL_REQUEST_KIND,
        PROVIDER,
        "Open pull request",
        PullRequestRecord,
        requires=(
            GitHub.METADATA_READ,
            GitHub.CONTENTS_READ,
            GitHub.PULL_REQUESTS_READ,
            GitHub.CHECKS_READ,
            GitHub.COMMIT_STATUSES_READ,
            GitHub.ACTIONS_READ,
        ),
        parts=(PULL_STATES,),
        title=lambda record: f"{record.get('repository', '')}#{record.get('number', '')}",
        describe=lambda record: str(record.get("title", "")),
        read_with=REPOSITORY_KIND,
        relation="Open pull request of",
        console=lambda record: str(record.get("url", "")),
        from_last=carry_held,
    ),
    ObservationSpec(
        PROFILE_KIND,
        PUBLIC_PROVIDER,
        "GitHub profile",
        ProfileRecord,
        parts=(AVATAR,),
        title=lambda record: str(record.get("login", "")),
        relation="Public profile of",
        console=lambda record: str(record.get("url", "")),
        every=_PROFILE_EVERY,
    ),
    ObservationSpec(
        DEPENDENCY_RUN_KIND,
        PROVIDER,
        "Dependency update run",
        DependencyRunRecord,
        # The token handed to the run: the App's Renovate grant.
        requires=(
            GitHub.CONTENTS_WRITE,
            GitHub.PULL_REQUESTS_WRITE,
            GitHub.ISSUES_WRITE,
            GitHub.COMMIT_STATUSES_WRITE,
            GitHub.WORKFLOWS_WRITE,
            GitHub.DEPENDABOT_ALERTS_READ,
        ),
        title=lambda record: str(record.get("owner", "")),
        describe=lambda record: (
            f"{counted(len(record.get('repositories') or ()), 'repository', 'repositories')}"
            f" · {dependency_updates.seen(str(record.get('outcome', ''))).label}"
        ),
        relation="Dependency updates of",
        at=lambda record: str(record.get("finished_at") or record.get("started_at") or ""),
        every=_DEPENDENCY_RUN_EVERY,
    ),
    ObservationSpec(
        ALLOWANCE_KIND,
        PROVIDER,
        "GitHub hourly allowance",
        AllowanceRecord,
        # GitHub's rate limit answer is free and needs no permission.
        title=lambda record: str(record.get("account", "") or record.get("installation", "")),
        describe=_allowance,
        relation="Hourly allowance of",
        from_last=with_others_spent,
    ),
    ObservationSpec(
        WEBHOOK_KIND,
        PROVIDER,
        "GitHub App webhook",
        WebhookRecord,
        # Two calls a sweep, each the App's own, signed with its key: they
        # need no permission and spend no installation's allowance.
        title=lambda record: str(record.get("app", "")),
        relation="Webhook of",
        console=app_settings,
    ),
)
