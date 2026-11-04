"""Readings of GitHub: each repository the App's installation covers, the
public profile of each account a sign-in names, and how each dependency update
run ended. What a repository's record holds is docs/Derived-Facts.md#github.

Which repositories is the installation's own list, so choosing them on GitHub
is the configuration. Secrets, variables' values and log contents are never
read, so the schema has nowhere to put them.
"""

from collections.abc import Iterable, Mapping
from datetime import timedelta
from typing import Annotated, Any, Literal

from pydantic import Field
from pydantic.json_schema import SkipJsonSchema

lazy from hq.platform.application.moments import when
lazy from hq.platform.application.timestamps import moment

from .contract import ObservationRecord, ObservationSpec, ReadingPart, Stamp

PROVIDER = "github_app"
REPOSITORY_KIND = "github.repository"
# How often each repository is read in full when GitHub's change query sees
# nothing new: the bound on how stale what the query cannot see gets (alerts,
# variables, runners, environments, branch rules, access, artifacts).
FULL_READ_EVERY = timedelta(hours=1)

# Refusable one at a time: a private repository on a free plan has no code
# scanning, and that should not hide its pull requests.
CODE_SCANNING = ReadingPart("code_scanning", "Code scanning alerts", requires=("code scanning alerts: read",))
DEPENDABOT = ReadingPart("dependabot", "Dependabot alerts", requires=("Dependabot alerts: read",))
# GitHub calls this secret scanning. Named for what it finds, leaked credentials,
# because this vocabulary reaches the topology, which carries no such word.
LEAKED_CREDENTIALS = ReadingPart(
    "leaked_credentials", "Leaked credential alerts", requires=("leaked credential alerts: read",)
)
BRANCH_RULES = ReadingPart("branch_rules", "Branch rules", requires=("administration: read",))
ENVIRONMENTS = ReadingPart("environments", "Environments", requires=("administration: read",))
RUNNERS = ReadingPart("runners", "Self-hosted runners", requires=("administration: read",))
IMAGES = ReadingPart("images", "Container images", requires=("packages: read",))
ACCESS = ReadingPart("access", "Access and Actions policy", requires=("administration: read",))
VARIABLES = ReadingPart("variables", "Actions variables", requires=("Actions variables: read",))
# Each workflow's ``uses:`` lines and the commit each tag names now: what a
# pinning fix replaces them with.
WORKFLOW_PINS = ReadingPart("workflow_pins", "Workflow action pins", requires=("contents: read",))
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
)


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
    checks: dict[str, Any] = {}
    # ``{number, title, url, author, draft, created_at, updated_at, head, branch,
    # labels, checks}`` per open pull request, newest first; ``checks`` is the
    # head's ``{state, total, failing, running, names}`` for a pull request
    # Renovate opened (its branch under ``renovate/``) and None for any other.
    pull_requests: list[dict[str, Any]] = []
    # The check names the newest pull request runs.
    pull_request_checks: list[str] = []
    runs: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    release: dict[str, Any] | None = None
    deployments: list[dict[str, Any]] = []
    # Open alerts counted by part and severity: ``{"dependabot": {"high": 2}}``.
    alerts: dict[str, Any] = {}
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
    # What GitHub's change query answered when this record was read, hashed,
    # and when it was read in full: the plan HQ sends the controller.
    fingerprint: str = ""
    read_at: Stamp = None
    # Sent in place of a record the change query found unchanged: the record
    # HQ holds stands (``carry_held``), and none is stored without one.
    carried: bool = False


def carry_held(last: Iterable[Mapping[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The records, each carried one replaced by the one HQ holds of the
    same repository, and dropped where HQ holds none."""

    held = {str(record.get("repository", "")): dict(record) for record in last}
    kept = []
    for record in records:
        if not record.get("carried"):
            kept.append(record)
        elif (standing := held.get(str(record.get("repository", "")))) is not None:
            kept.append(standing)
    return kept


# What GitHub shows anyone, so no credential reads it: the provider is GitHub
# itself, not a connection.
PUBLIC_PROVIDER = "github"
PROFILE_KIND = "github.profile"
# GitHub rations anonymous calls by the hour, and a profile changes slowly, so
# it is read this often and when somebody asks.
PROFILE_EVERY = timedelta(hours=6)
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
DEPENDENCY_RUN_EVERY = timedelta(days=1)
# An account and a repository as a run names them: GitHub's rules, lower-cased.
# The controller reads both from this record's schema in the bridge contract.
OWNER = r"^[a-z0-9-]{1,39}$"
REPOSITORY = r"^([a-z0-9-]{1,39})/[a-z0-9._-]{1,100}$"


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


ALLOWANCE_KIND = "github.allowance"
# Others' spend within one hour that is past what the deploy pipeline and the
# dependency update run take of it, a few requests a deploy and some hundreds
# a day: more than that is something else holding the App's key or a token.
OTHERS_SPENT_ATTENTION = 1000
# The fact a connection's node carries once others' spend passes it, as
# ``account|requests|since|until``.
OTHERS_SPENT = "github-others-spent"


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


def _allowance_facts(record: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    spent = record.get("others_spent")
    if spent is None or int(spent) < OTHERS_SPENT_ATTENTION:
        return ()
    value = "|".join(
        (str(record.get("account", "")), str(spent), str(record.get("others_since", "")), str(record["started_at"]))
    )
    return ((OTHERS_SPENT, value),)


def _allowance(record: Mapping[str, Any]) -> str:
    """The hour as one line: "4,210 of 5,000 left, resets 2:00 PM; HQ spent 12, others 30"."""

    said = f"{int(record.get('remaining') or 0):,} of {int(record.get('limit') or 0):,} left"
    if (resets := moment(record.get("resets_at"))) is not None:
        said += f", resets {when(resets)}"
    said += f"; HQ spent {int(record.get('spent') or 0):,}"
    if (others := record.get("others_spent")) is not None:
        said += f", others {int(others):,}"
        if (since := moment(record.get("others_since"))) is not None:
            said += f" since {when(since)}"
    return said


OBSERVATIONS: tuple[ObservationSpec, ...] = (
    ObservationSpec(
        REPOSITORY_KIND,
        PROVIDER,
        "GitHub repository",
        RepositoryRecord,
        requires=(
            "metadata: read",
            "contents: read",
            "checks: read",
            "pull requests: read",
            "actions: read",
            "deployments: read",
            "code scanning alerts: read",
            "Dependabot alerts: read",
            "leaked credential alerts: read",
            "administration: read",
            "packages: read",
            "Actions variables: read",
        ),
        parts=PARTS,
        title=lambda record: str(record.get("repository", "")),
        relation="Built from repository",
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
        every=PROFILE_EVERY,
    ),
    ObservationSpec(
        DEPENDENCY_RUN_KIND,
        PROVIDER,
        "Dependency update run",
        DependencyRunRecord,
        # The token handed to the run: the App's Renovate grant.
        requires=(
            "contents: write",
            "pull requests: write",
            "issues: write",
            "commit statuses: write",
            "workflows: write",
            "Dependabot alerts: read",
        ),
        title=lambda record: str(record.get("owner", "")),
        relation="Dependency updates of",
        every=DEPENDENCY_RUN_EVERY,
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
        facts=_allowance_facts,
        from_last=with_others_spent,
    ),
)
