"""What GitHub says about the repositories HQ is built from, and what needs you.

Read from the GitHub App's ``github.repository`` reading, which the controller
takes under a read-only token per repository; nothing here calls GitHub. A
project joins to its repository by its URL, and the action queue takes the
few things on GitHub that wait on a person: a deploy held for approval, a
default branch failing, a serious alert, an admission about to lapse.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from hq.domains.control_plane.observations.github import REPOSITORY_KIND

from .containers import SERIOUS
from .derivations import reached, whole
from .entity_links import web_url
from .expiry import days_until
from .freshness import Freshness, freshness
from .github_public import github_repository
from .item_help import cannot_help, commands
from .projection import read_once
from .timestamps import moment
from .ui import Insight, counted
from .workflow_contracts import ActionLink

# An admission artifact that lapses stops the composition admitting its
# extension, and nothing else says so until a deploy fails.
ARTIFACT_ATTENTION_DAYS = 14
ARTIFACT_SERIOUS_DAYS = 3
# A deploy held for approval is fine for a while; hours later it is a
# scheduled run about to be cancelled.
WAITING_SERIOUS_AFTER = timedelta(hours=1)
# A run's conclusions that fail it, as the reading's checks count them.
FAILED = frozenset({"failure", "timed_out", "action_required", "startup_failure"})


@dataclass(frozen=True, slots=True)
class Repository:
    name: str
    record: Mapping[str, Any]
    observed_at: datetime | None
    # Labels of the parts GitHub would not answer for this repository.
    refused: tuple[str, ...] = field(default=())

    def __getattr__(self, item: str) -> Any:
        """A field of the record, or the reading schema's default for it."""

        from hq.domains.control_plane.observations.github import RepositoryRecord

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
    def serious_alerts(self) -> int:
        return sum(counts.get(level, 0) for counts in (self.record.get("alerts") or {}).values() for level in SERIOUS)

    @property
    def severities(self) -> list[tuple[int, str]]:
        """Open alerts by severity, worst first: ``[(2, "high"), (1, "low")]``."""

        order = ("critical", "high", "medium", "moderate", "low", "note", "warning", "error", "leaked", "unknown")
        totals: dict[str, int] = {}
        for counts in (self.record.get("alerts") or {}).values():
            for level, n in counts.items():
                totals[level] = totals.get(level, 0) + n
        return sorted(
            ((n, level) for level, n in totals.items()),
            key=lambda item: order.index(item[1]) if item[1] in order else len(order),
        )

    @property
    def open_alerts(self) -> int:
        return sum(sum(counts.values()) for counts in (self.record.get("alerts") or {}).values())

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
    from .facts import snapshots_of

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

    parts = github_repository(url)
    return repositories().get("/".join(parts)) if parts else None


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
        action="Open the failing run" if one else "Open checks",
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
        action="Open the deploy",
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
    if not repo.serious_alerts:
        return []
    alerts = counted(repo.serious_alerts, "serious alert", "serious alerts")
    item = Insight(
        status="serious" if _has(repo, "critical") else "attention",
        eyebrow="GitHub",
        family="Security alerts",
        key=f"github-alerts:{repo.name}",
        title=f"{alerts} in {repo.short}",
        value=str(repo.serious_alerts),
        body="High or critical, open.",
        action="Open security",
        url=f"{repo.url}/security",
        workflow=cannot_help(
            f"github-alerts:{repo.name}",
            "An alert closes when the code is upgraded. HQ never writes to the repository.",
        ),
        observed_at=repo.observed_at,
    )
    return [(item, f"{alerts} were open in {repo.short}")]


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


def _has(repo: Repository, level: str) -> bool:
    return any(counts.get(level, 0) for counts in (repo.record.get("alerts") or {}).values())


def build_of(image: str) -> dict[str, Any] | None:
    """What GitHub says about a running image reference: the repository it is
    built from, and whether its digest carries a cosign signature.

    ``ghcr.io/owner/name/composition@sha256:…``, as a container reports it.
    None for an image not pinned by digest or not read through the App.
    """

    from .images import GHCR, ImageRef

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
