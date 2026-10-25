"""Readings of GitHub: each repository the App's installation covers, the
public profile of each account a sign-in names, and how each dependency update
run ended.

One record per repository, as the App's read-only token sees it: the default
branch's head and its checks, open pull requests, the latest run of each
workflow and any waiting on an approval, the latest release, recent
deployments, open security alerts, and the build artifacts with an expiry.

Which repositories is the installation's own list, so choosing them on GitHub
is the configuration. Secrets, variables' values and log contents are never
read, so the schema has nowhere to put them.
"""

from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field

from .contract import ObservationRecord, ObservationSpec, ReadingPart, Stamp

PROVIDER = "github_app"
REPOSITORY_KIND = "github.repository"

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
    # Who and what can reach the repository, and how its Actions may run.
    access: dict[str, Any] | None = None
    # Names only: a variable's value is never read.
    variables: list[str] | None = None
    # ``{path, uses, action, ref, sha}`` per ``uses:`` line not pinned to a
    # commit, in the workflows and the local composite actions; ``sha`` is the
    # commit its ref names, or "" where it could not be read.
    pins: list[dict[str, Any]] | None = None
    # Each ``uses:`` of a reusable workflow another repository holds, pinned or
    # not: what it uses in turn is not read here.
    called_workflows: list[str] | None = None


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
)
