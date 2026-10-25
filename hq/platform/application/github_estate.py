"""What GitHub says about the repositories HQ is built from, and what needs you.

Read from the GitHub App's ``github.repository`` reading, which the controller
takes under a read-only token per repository; nothing here calls GitHub. A
project joins to its repository by its URL, and the action queue takes the
few things on GitHub that wait on a person: a deploy held for approval, a
default branch failing, a serious alert, an admission about to lapse.
"""

from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from hq.domains.control_plane.observations.contract import ReadingPart
from hq.domains.control_plane.observations.github import (
    CODE_SCANNING,
    DEPENDABOT,
    LEAKED_CREDENTIALS,
    REPOSITORY_KIND,
    RepositoryRecord,
)

from .containers import SERIOUS
from .derivations import reached, whole
from .display_words import display
from .entity_links import web_url
from .expiry import days_until
from .freshness import Freshness, freshness
from .item_help import cannot_help, commands
from .moments import ago
from .projection import read_once
from .timestamps import moment
from .ui import Insight, counted
from .workflow_contracts import ActionLink
lazy from .facts import snapshots_of
lazy from .images import GHCR, ImageRef
lazy from .projects import projects_by_repository, repository_key

# An admission artifact that lapses stops the composition admitting its
# extension, and nothing else says so until a deploy fails.
ARTIFACT_ATTENTION_DAYS = 14
ARTIFACT_SERIOUS_DAYS = 3
# A deploy held for approval is fine for a while; hours later it is a
# scheduled run about to be cancelled.
WAITING_SERIOUS_AFTER = timedelta(hours=1)
# A run's conclusions that fail it, as the reading's checks count them.
FAILED = frozenset({"failure", "timed_out", "action_required", "startup_failure"})
# The one severity secret scanning reports: a credential found in the code.
LEAKED = "leaked"
# Alert severities, worst first, across every part that reports them.
SEVERITY_ORDER = ("critical", "high", "medium", "moderate", "low", "note", "warning", "error", LEAKED, "unknown")
# The alerts that put a repository in the action queue: a leaked credential is
# usable by whoever reads the code, whatever its scanner calls it.
SERIOUS_ALERTS = (*SERIOUS, LEAKED)
# GitHub's list of each part's open alerts, under the repository's
# ``/security``: the overview there leads with a security policy, not alerts.
ALERT_PAGES: Mapping[ReadingPart, str] = {
    CODE_SCANNING: "code-scanning",
    DEPENDABOT: "dependabot",
    LEAKED_CREDENTIALS: "secret-scanning",
}
# The tone of the head's combined check state.
_CHECKS_TONES = {"failure": "serious", "success": "good", "pending": "attention"}
# The tone of a deployment's state; any other state of one is still going.
_DEPLOY_TONES = {"success": "good", "failure": "serious", "error": "serious"}
# The branches Renovate opens its pull requests from.
UPDATE_BRANCH = "renovate/"


@display
class WorkflowRun:
    """A workflow's newest run, as its chip on a repository's panel shows it."""

    name: str
    url: str
    # What it came to, or where it is while it has not finished: "Success".
    state: str
    # The run's color: good, serious, attention, or none while it says nothing.
    tone: str
    # Shown on hover: what it came to, then its branch, commit and age.
    tip: str
    # Read out after its name: what it came to, its branch, and its age.
    said: str


def workflow_run(run: Mapping[str, Any]) -> WorkflowRun:
    """One run of the reading's ``runs``, as its chip says it."""

    conclusion, status = str(run.get("conclusion") or ""), str(run.get("status") or "")
    if conclusion == "success":
        tone = "good"
    elif conclusion in ("failure", "timed_out"):
        tone = "serious"
    elif status != "completed":
        tone = "attention"
    else:
        tone = ""
    word = (conclusion or status).replace("_", " ")
    state = word.capitalize()
    branch = str(run.get("branch") or "")
    created = moment(str(run.get("created_at") or ""))
    age = ago(created) if created else ""
    where = " · ".join(part for part in (branch, str(run.get("sha") or "")[:7], age) if part)
    return WorkflowRun(
        name=str(run.get("name") or "Workflow"),
        url=web_url(run.get("url")),
        state=state,
        tone=tone,
        tip=f"{state}\n{where}" if where else state,
        said=", ".join(part for part in (word, branch, age) if part),
    )


def _severity_rank(level: str) -> int:
    return SEVERITY_ORDER.index(level) if level in SEVERITY_ORDER else len(SEVERITY_ORDER)


@dataclass(frozen=True, slots=True)
class Repository:
    name: str
    record: Mapping[str, Any]
    observed_at: datetime | None
    # Labels of the parts GitHub would not answer for this repository.
    refused: tuple[str, ...] = field(default=())

    def __getattr__(self, item: str) -> Any:
        """A field of the record, or the reading schema's default for it."""

        if item in self.record:
            return self.record[item]
        declared = RepositoryRecord.model_fields.get(item)
        if declared is None:
            raise AttributeError(item)
        return declared.get_default(call_default_factory=True)

    @property
    def reading(self) -> Freshness:
        return freshness(REPOSITORY_KIND, self.observed_at)

    @property
    def short(self) -> str:
        return self.name.split("/", 1)[-1]

    @property
    def failing(self) -> bool:
        return self.record.get("checks", {}).get("state") == "failure"

    @property
    def checks_tone(self) -> str:
        """The head's checks as a tone: none while nothing runs on it."""

        return _CHECKS_TONES.get(self.record.get("checks", {}).get("state"), "")

    @property
    def failing_runs(self) -> tuple[ActionLink, ...]:
        """A link to each workflow run that failed on the default branch's head,
        as far as the reading holds them: its newest run of each workflow."""

        sha = (self.head or {}).get("sha", "")
        found = []
        for run in self.record.get("runs") or ():
            url = web_url(run.get("url"))
            if sha and url and run.get("sha") == sha and run.get("conclusion") in FAILED:
                name = run.get("name") or "Workflow"
                found.append(
                    ActionLink("failing-run", f"{name} run", "read", url, reason=f"{name} failed on this commit.")
                )
        return tuple(found)

    @property
    def workflow_runs(self) -> tuple[WorkflowRun, ...]:
        """Each workflow's newest run, as the repository's panel shows it."""

        return tuple(workflow_run(run) for run in self.record.get("runs") or ())

    @property
    def serious_alerts(self) -> int:
        return sum(self.alerts_at(level) for level in SERIOUS_ALERTS)

    @property
    def leaked_credentials(self) -> int:
        return self.alerts_at(LEAKED)

    def alerts_at(self, level: str) -> int:
        return sum(counts.get(level, 0) for counts in (self.record.get("alerts") or {}).values())

    @property
    def severities(self) -> list[tuple[int, str]]:
        """Open alerts by severity, worst first: ``[(2, "high"), (1, "low")]``."""

        totals: dict[str, int] = {}
        for counts in (self.record.get("alerts") or {}).values():
            for level, n in counts.items():
                totals[level] = totals.get(level, 0) + n
        return sorted(((n, level) for level, n in totals.items()), key=lambda item: _severity_rank(item[1]))

    @property
    def alerts_tone(self) -> str:
        """The open alerts as a tone: none where GitHub offers no alerts."""

        if self.serious_alerts:
            return "serious"
        if self.open_alerts:
            return "attention"
        return "good" if self.record.get("alerts") else ""

    @property
    def update_pulls(self) -> list[dict[str, Any]]:
        """Open pull requests Renovate opened, newest first, each with when it
        was opened (``opened``) and its age in whole days (``age_days``)."""

        found = []
        for pull in self.record.get("pull_requests") or ():
            if str(pull.get("branch") or "").startswith(UPDATE_BRANCH):
                opened = moment(pull.get("created_at", ""))
                age = max(0, whole(opened, timedelta(days=1))) if opened else None
                found.append({**pull, "opened": opened, "age_days": age})
        return found

    @property
    def other_pulls(self) -> list[dict[str, Any]]:
        """Open pull requests a person or another tool opened."""

        return [
            pull
            for pull in self.record.get("pull_requests") or ()
            if not str(pull.get("branch") or "").startswith(UPDATE_BRANCH)
        ]

    @property
    def oldest_update_days(self) -> int | None:
        return max((pull["age_days"] for pull in self.update_pulls if pull["age_days"] is not None), default=None)

    @property
    def unmatched_alerts(self) -> list[dict[str, Any]]:
        """Open Dependabot alerts no open update pull request answers, worst first.

        A name match: the reading does not say which package a pull request
        updates, so an alert is answered when its package's name appears in an
        update's branch or title, ignoring case.
        """

        updates = [f"{pull.get('branch') or ''} {pull.get('title') or ''}".lower() for pull in self.update_pulls]
        found = []
        for alert in self.record.get("dependabot_alerts") or ():
            package = str(alert.get("package") or "").lower()
            if not package or not any(package in text for text in updates):
                found.append(alert)
        return sorted(found, key=lambda alert: _severity_rank(str(alert.get("severity") or "")))

    @property
    def open_alerts(self) -> int:
        return sum(sum(counts.values()) for counts in (self.record.get("alerts") or {}).values())

    def alert_links(self, levels: Collection[str] = ()) -> tuple[ActionLink, ...]:
        """A link to GitHub's list of each part's open alerts, the part holding
        the worst first; with ``levels``, only the parts holding one of those."""

        found = []
        for part, page in ALERT_PAGES.items():
            held = [level for level, n in ((self.record.get("alerts") or {}).get(part.name) or {}).items() if n]
            if held and (not levels or any(level in levels for level in held)):
                link = ActionLink(f"alerts-{part.name}", part.label, "read", f"{self.url}/security/{page}")
                found.append((min(map(_severity_rank, held)), link))
        return tuple(link for _, link in sorted(found, key=lambda item: item[0]))

    @property
    def alerts_url(self) -> str:
        """Where the open alerts are listed: the worst part's list, or the
        security overview when none is open."""

        links = self.alert_links()
        return links[0].url if links else f"{self.url}/security"

    @property
    def unrequired_checks(self) -> list[str]:
        """Checks that run on a pull request but the default branch's rules do not
        require: the ones that could stop a merge and do not."""

        rules = self.record.get("rules") or {}
        required = set(rules.get("required_checks") or ())
        return [name for name in self.record.get("pull_request_checks") or () if name not in required]

    @property
    def production_environment(self) -> Mapping[str, Any] | None:
        return next((item for item in self.record.get("environments") or () if item.get("name") == "production"), None)

    @property
    def production_verified(self) -> bool | None:
        """Whether production's last deploy passed every check it ran before
        deploying (a signature verified, say): None when it ran none."""

        steps = (self.production or {}).get("verified") or []
        if not steps:
            return None
        return all(step.get("conclusion") == "success" for step in steps)

    @property
    def production(self) -> Mapping[str, Any] | None:
        return next(
            (item for item in self.record.get("deployments") or () if item.get("environment") == "production"),
            None,
        )

    @property
    def production_tone(self) -> str:
        """Production's last deploy as a tone: none where there is none."""

        if not self.production:
            return ""
        return _DEPLOY_TONES.get(self.production.get("state"), "attention")

    @property
    def verified_tone(self) -> str:
        """``production_verified`` as a tone: none where the deploy ran no check."""

        return {True: "good", False: "serious"}.get(self.production_verified, "")

    @property
    def deployed_cleanly(self) -> bool:
        """Whether production has a deploy that did not fail its checks: one
        that ran none is still the deploy."""

        return bool(self.production) and self.production_verified is not False

    @property
    def undeployed(self) -> bool | None:
        """Whether the default branch has moved past what production runs: None
        when either commit was not read, so an unread deploy is never "current"."""

        head, deployed = str((self.head or {}).get("sha", "")), str((self.production or {}).get("sha", ""))
        if not head or not deployed:
            return None
        return head != deployed

    def expiring(self, *, within_days: int = ARTIFACT_ATTENTION_DAYS) -> list[dict[str, Any]]:
        """Admission artifacts that lapse within ``within_days``, soonest first."""

        found = []
        for item in self.record.get("artifacts") or ():
            when = moment(item.get("expires_at", ""))
            if when is None or "admission" not in item.get("name", ""):
                continue
            days = days_until(when)
            if days <= within_days:
                found.append({**item, "days": max(days, 0)})
        return found


def repositories() -> dict[str, Repository]:
    """Every repository the App reads, by ``owner/name``. Read once per projection."""

    return read_once("github_estate.repositories", _load)


def _load() -> dict[str, Repository]:
    found: dict[str, Repository] = {}
    for snapshot in snapshots_of(REPOSITORY_KIND):
        refused_by_repo: dict[str, list[str]] = {}
        for item in getattr(snapshot, "refused_parts", None) or ():
            if isinstance(item, Mapping):
                refused_by_repo.setdefault(str(item.get("scope", "")), []).append(str(item.get("part", "")))
        for record in snapshot.records or ():
            if isinstance(record, Mapping) and record.get("repository"):
                name = str(record["repository"])
                found[name] = Repository(name, record, snapshot.observed_at, tuple(refused_by_repo.get(name, ())))
    return found


def repository_for(url: str) -> Repository | None:
    """The repository a project's URL names, when the App reads it."""

    def load() -> dict[str, Repository]:
        return {repository_key(name): repo for name, repo in repositories().items()}

    key = repository_key(url)
    return read_once("github_estate.by_key", load).get(key) if key else None


def attention() -> tuple[Insight, ...]:
    items: list[Insight] = []
    for repo in sorted(repositories().values(), key=lambda item: item.name):
        found = [*_waiting(repo), *_failing(repo), *_unverified(repo), *_alerts(repo), *_lapsing(repo)]
        items.extend(_as_read(repo, item, was) for item, was in found)
    return tuple(items)


def _as_read(repo: Repository, item: Insight, was: str) -> Insight:
    """``item`` as of the reading it is drawn from.

    Once that reading is out of date the item no longer says the condition
    holds: it says what was read (``was``, in the past tense) and only as a
    notice, so an old reading never stands in the queue as a current fact.
    """

    if not repo.reading.stale:
        return item
    # Not "neutral": the queue leaves those out, and an old reading still
    # stands in it, as what was last known.
    return replace(item, title=was, status="attention", notice=True)


def _failing(repo: Repository) -> list[tuple[Insight, str]]:
    if not repo.failing:
        return []
    failing = repo.record["checks"].get("failing") or []
    runs = repo.failing_runs
    one = runs[0] if len(runs) == 1 else None
    item = Insight(
        status="serious",
        eyebrow="GitHub",
        family="Failing workflows",
        key=f"github-failing:{repo.name}",
        title=f"{repo.short} is failing on {repo.default_branch}",
        value=str(len(failing) or 1),
        body=", ".join(failing[:4]) + (f" and {len(failing) - 4} more" if len(failing) > 4 else ""),
        action="Open run" if one else "Open checks",
        url=one.url if one else f"{repo.url}/commit/{repo.head.get('sha', '')}",
        workflow=_failing_help(repo, runs),
        observed_at=repo.observed_at,
        links=() if one else runs,
    )
    return [(item, f"{repo.short} was failing on {repo.default_branch}")]


def _unverified(repo: Repository) -> list[tuple[Insight, str]]:
    if repo.production_verified is not False:
        return []
    failed = [step["name"] for step in repo.production["verified"] if step.get("conclusion") != "success"]
    item = Insight(
        status="serious",
        eyebrow="GitHub",
        family="Unverified commits",
        key=f"github-unverified:{repo.name}",
        title=f"{repo.short}'s last deploy did not pass its own verification",
        value=str(len(failed)),
        body=", ".join(failed),
        action="Open deploy",
        url=web_url(repo.production.get("url")) or repo.url,
        workflow=cannot_help(
            f"github-unverified:{repo.name}",
            "HQ sees which step failed, not why. The cause is in the deploy's log, "
            "and the fix in the repository, which HQ never writes to.",
        ),
        observed_at=repo.observed_at,
    )
    return [(item, f"{repo.short}'s last deploy had not passed its own verification")]


def _alerts(repo: Repository) -> list[tuple[Insight, str]]:
    kinds = repo.alert_links(SERIOUS_ALERTS)
    if not kinds:
        return []
    one, many, body = _alerts_said(repo)
    alerts = counted(repo.serious_alerts, one, many)
    unmatched = sum(1 for alert in repo.unmatched_alerts if alert.get("severity") in SERIOUS_ALERTS)
    updates = _updates_url(repo) if unmatched else ""
    if unmatched:
        body += f" {counted(unmatched, 'has', 'have')} no update pull request."
    item = Insight(
        status="serious" if repo.leaked_credentials or repo.alerts_at("critical") else "attention",
        eyebrow="GitHub",
        family="Security alerts",
        key=f"github-alerts:{repo.name}",
        title=f"{alerts} in {repo.short}",
        value=str(repo.serious_alerts),
        body=body,
        action="Open updates" if updates else "Open alerts",
        url=updates or kinds[0].url,
        links=kinds if updates or len(kinds) > 1 else (),
        workflow=cannot_help(
            f"github-alerts:{repo.name}",
            "An alert closes when the code is upgraded. HQ never writes to the repository.",
        ),
        observed_at=repo.observed_at,
    )
    return [(item, f"{counted(repo.serious_alerts, f'{one} was', f'{many} were')} open in {repo.short}")]


def _alerts_said(repo: Repository) -> tuple[str, str, str]:
    """What the alert item counts, as one and as many, and what kind they are."""

    leaked = repo.leaked_credentials
    if leaked == repo.serious_alerts:
        return "leaked credential", "leaked credentials", "Found by secret scanning, open."
    body = "High or critical, open."
    if leaked:
        body += f" {counted(leaked, 'is a leaked credential', 'are leaked credentials')}."
    return "serious alert", "serious alerts", body


def _updates_url(repo: Repository) -> str:
    """The Updates section of the project the repository is, when one is."""

    project = projects_by_repository().get(repository_key(repo.name))
    return f"{project.get_absolute_url()}#updates" if project else ""


def _lapsing(repo: Repository) -> list[tuple[Insight, str]]:
    return [
        (
            Insight(
                status="serious" if artifact["days"] <= ARTIFACT_SERIOUS_DAYS else "attention",
                eyebrow="GitHub",
                family="Workflow artifacts",
                key=f"github-artifact:{repo.name}:{artifact['name']}",
                title=f"{repo.short}'s admission lapses in {artifact['days']} days",
                value=str(artifact["days"]),
                body="Once it lapses, HQ's next build leaves it out. Run its admission again to renew it.",
                action="Open actions",
                url=f"{repo.url}/actions",
                workflow=cannot_help(
                    f"github-artifact:{repo.name}:{artifact['name']}",
                    "The reading names the artifact but not the workflow that makes it, so HQ "
                    "cannot name the run to repeat.",
                ),
                observed_at=repo.observed_at,
            ),
            f"{repo.short}'s admission was due to lapse in {artifact['days']} days",
        )
        for artifact in repo.expiring()
    ]


def _failing_help(repo: Repository, runs: tuple[ActionLink, ...]) -> Any:
    """Why HQ cannot fix it, and, where the reading does not hold the failing
    runs, the command that lists them."""

    key = f"github-failing:{repo.name}"
    reason = "HQ reads which checks fail, not their logs, and the fix is a change to the code or a workflow."
    sha = repo.head.get("sha", "")
    if runs or not sha:
        return cannot_help(key, reason)
    return commands(
        key,
        (("See the failing runs", f"gh run list -R {repo.name} --commit {sha} --status failure"),),
        reason=reason,
    )


def _waiting(repo: Repository) -> list[tuple[Insight, str]]:
    items = []
    for run in repo.waiting or ():
        started = moment(run.get("created_at", ""))
        hours = max(0, whole(started, timedelta(hours=1))) if started else 0
        serious = bool(started) and reached(started + WAITING_SERIOUS_AFTER)
        where = ", ".join(run.get("environments") or ()) or "an environment"
        name = run.get("name") or "A run"
        item = Insight(
            status="serious" if serious else "attention",
            eyebrow="GitHub",
            family="Deployments awaiting approval",
            key=f"github-waiting:{repo.name}:{run.get('id')}",
            value=f"{hours}h",
            title=f"{name} waits for your approval to deploy to {where}",
            body=f"{repo.short} · {run.get('sha', '')[:7]}",
            action="Review deployment",
            url=web_url(run.get("url")) or repo.url,
            workflow=cannot_help(
                f"github-waiting:{repo.name}:{run.get('id')}",
                "Only a required reviewer can approve a deployment, and HQ reads GitHub with read-only tokens.",
            ),
            observed_at=repo.observed_at,
        )
        items.append((item, f"{name} was waiting for your approval to deploy to {where}"))
    return items


def build_of(image: str) -> dict[str, Any] | None:
    """What GitHub says about a running image reference: the repository it is
    built from, and whether its digest carries a cosign signature.

    ``ghcr.io/owner/name/composition@sha256:…``, as a container reports it.
    None for an image not pinned by digest or not read through the App.
    """

    parsed = ImageRef.parse(image)
    if parsed is None or parsed.registry != GHCR or not parsed.digest:
        return None
    name, digest = parsed.repository.lower(), parsed.digest.removeprefix("sha256:")
    for repo in repositories().values():
        for item in repo.images or ():
            if item.get("name") == name:
                return {
                    "repository": repo.name,
                    "url": repo.url,
                    "image": name,
                    "signed": digest in (item.get("signed") or ()),
                }
    return None
