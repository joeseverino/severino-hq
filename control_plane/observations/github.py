"""Readings through the GitHub App: each repository its installation covers.

One record per repository, as the App's read-only token sees it: the default
branch's head and its checks, open pull requests, the latest run of each
workflow and any waiting on an approval, the latest release, recent
deployments, open security alerts, and the build artifacts with an expiry.

Which repositories is the installation's own list, so choosing them on GitHub
is the configuration. Secrets, variables' values and log contents are never
read, so the schema has nowhere to put them.
"""

from __future__ import annotations

from typing import Any

from .contract import ObservationRecord, ObservationSpec, ReadingPart

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
    CODE_SCANNING, DEPENDABOT, LEAKED_CREDENTIALS, BRANCH_RULES, ENVIRONMENTS, RUNNERS, IMAGES, ACCESS,
    VARIABLES, WORKFLOW_PINS,
)


class RepositoryRecord(ObservationRecord):
    connection_ref: str
    repository: str
    private: bool = False
    url: str = ""
    default_branch: str = ""
    pushed_at: str = ""
    head: dict[str, Any] = {}
    checks: dict[str, Any] = {}
    pull_requests: list[dict[str, Any]] = []
    pull_request_checks: list[str] = []
    runs: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    release: dict[str, Any] | None = None
    deployments: list[dict[str, Any]] = []
    alerts: dict[str, Any] = {}
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
)
