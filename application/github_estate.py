"""What GitHub says about the repositories HQ is built from, and what needs you.

Read from the GitHub App's ``github.repository`` reading, which the controller
takes under a read-only token per repository; nothing here calls GitHub. A
project joins to its repository by its URL, and the action queue takes the
few things on GitHub that wait on a person: a deploy held for approval, a
default branch failing, a serious alert, an admission about to lapse.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

from django.utils import timezone

from .expiry import days_until
from .github_public import github_repository
from .projection import read_once
from .ui import Insight, counted, moment

REPOSITORY_KIND = "github.repository"
# An admission artifact that lapses stops the composition admitting its
# extension, and nothing else says so until a deploy fails.
ARTIFACT_ATTENTION_DAYS = 14
ARTIFACT_SERIOUS_DAYS = 3
# A deploy held for approval is fine for a while; hours later it is a
# scheduled run about to be cancelled.
WAITING_SERIOUS_AFTER = timedelta(hours=1)
_SERIOUS_LEVELS = ("critical", "high")


@dataclass(frozen=True)
class Repository:
    name: str
    record: Mapping[str, Any]
    observed_at: datetime | None
    # Labels of the parts GitHub would not answer for this repository.
    refused: tuple[str, ...] = field(default=())

    def __getattr__(self, item: str) -> Any:
        """A field of the record, or the reading schema's default for it."""

        from control_plane.observations.github import RepositoryRecord

        if item in self.record:
            return self.record[item]
        declared = RepositoryRecord.model_fields.get(item)
        if declared is None:
            raise AttributeError(item)
        return declared.get_default(call_default_factory=True)

    @property
    def short(self) -> str:
        return self.name.split("/", 1)[-1]

    @property
    def failing(self) -> bool:
        return self.record.get("checks", {}).get("state") == "failure"

    @property
    def serious_alerts(self) -> int:
        return sum(
            counts.get(level, 0)
            for counts in (self.record.get("alerts") or {}).values()
            for level in _SERIOUS_LEVELS
        )

    @property
    def severities(self) -> list[tuple[int, str]]:
        """Open alerts by severity, worst first: ``[(2, "high"), (1, "low")]``."""

        order = ("critical", "high", "medium", "moderate", "low", "note", "warning", "error", "leaked", "unknown")
        totals: dict[str, int] = {}
        for counts in (self.record.get("alerts") or {}).values():
            for level, n in counts.items():
                totals[level] = totals.get(level, 0) + n
        return sorted(((n, level) for level, n in totals.items()), key=lambda item: order.index(item[1]) if item[1] in order else len(order))

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
                found[name] = Repository(
                    name, record, snapshot.observed_at, tuple(refused_by_repo.get(name, ()))
                )
    return found


def repository_for(url: str) -> Repository | None:
    """The repository a project's URL names, when the App reads it."""

    parts = github_repository(url)
    return repositories().get("/".join(parts)) if parts else None


def attention() -> tuple[Insight, ...]:
    items: list[Insight] = []
    for repo in sorted(repositories().values(), key=lambda item: item.name):
        items.extend(_waiting(repo))
        if repo.failing:
            failing = repo.record["checks"].get("failing") or []
            items.append(
                Insight(
                    status="serious",
                    eyebrow="GitHub",
                    key=f"github-failing:{repo.name}",
                    title=f"{repo.short} is failing on {repo.default_branch}",
                    value=str(len(failing) or 1),
                    body=", ".join(failing[:4]) + (f" and {len(failing) - 4} more" if len(failing) > 4 else ""),
                    action="Open checks",
                    url=f"{repo.url}/commit/{repo.head.get('sha', '')}",
                )
            )
        if repo.production_verified is False:
            failed = [step["name"] for step in repo.production["verified"] if step.get("conclusion") != "success"]
            items.append(
                Insight(
                    status="serious",
                    eyebrow="GitHub",
                    key=f"github-unverified:{repo.name}",
                    title=f"{repo.short}'s last deploy did not pass its own verification",
                    value=str(len(failed)),
                    body=", ".join(failed),
                    action="Open the deploy",
                    url=str(repo.production.get("url") or repo.url),
                )
            )
        if repo.serious_alerts:
            items.append(
                Insight(
                    status="serious" if _has(repo, "critical") else "attention",
                    eyebrow="GitHub",
                    key=f"github-alerts:{repo.name}",
                    title=f"{counted(repo.serious_alerts, 'serious alert', 'serious alerts')} in {repo.short}",
                    value=str(repo.serious_alerts),
                    body="High or critical, open.",
                    action="Open security",
                    url=f"{repo.url}/security",
                )
            )
        for artifact in repo.expiring():
            items.append(
                Insight(
                    status="serious" if artifact["days"] <= ARTIFACT_SERIOUS_DAYS else "attention",
                    eyebrow="GitHub",
                    key=f"github-artifact:{repo.name}:{artifact['name']}",
                    title=f"{repo.short}'s admission lapses in {artifact['days']} days",
                    value=str(artifact["days"]),
                    body="Composition stops admitting it when it does. A new admission run renews it.",
                    action="Open actions",
                    url=f"{repo.url}/actions",
                )
            )
    return tuple(items)


def _waiting(repo: Repository) -> list[Insight]:
    items = []
    for run in repo.waiting or ():
        started = moment(run.get("created_at", ""))
        waited = timezone.now() - started if started else timedelta(0)
        where = ", ".join(run.get("environments") or ()) or "an environment"
        items.append(
            Insight(
                status="serious" if waited >= WAITING_SERIOUS_AFTER else "attention",
                eyebrow="GitHub",
                key=f"github-waiting:{repo.name}:{run.get('id')}",
                value=f"{int(waited.total_seconds() // 3600)}h",
                title=f"{run.get('name') or 'A run'} waits for your approval to deploy to {where}",
                body=f"{repo.short} · {run.get('sha', '')[:7]}",
                action="Review deployment",
                url=str(run.get("url") or repo.url),
            )
        )
    return items


def _has(repo: Repository, level: str) -> bool:
    return any(counts.get(level, 0) for counts in (repo.record.get("alerts") or {}).values())


def build_of(image: str) -> dict[str, Any] | None:
    """What GitHub says about a running image reference: the repository it is
    built from, and whether its digest carries a cosign signature.

    ``ghcr.io/owner/name/composition@sha256:…``, as a container reports it.
    None for an image not pinned by digest or not read through the App.
    """

    reference, marker, digest = image.partition("@sha256:")
    if not marker or not reference.startswith("ghcr.io/"):
        return None
    name = reference.removeprefix("ghcr.io/").lower()
    for repo in repositories().values():
        for item in repo.images or ():
            if item.get("name") == name:
                return {"repository": repo.name, "url": repo.url, "image": name, "signed": digest in (item.get("signed") or ())}
    return None
