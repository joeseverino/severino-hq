"""What GitHub says about the repositories HQ is built from, and what needs you.

Read from the GitHub App's ``github.repository`` reading, which the controller
takes under a read-only token per repository, with the ``github.workflow_run``
and ``github.open_pull_request`` readings taken in the same pass; nothing here
calls GitHub. A
project joins to its repository by its URL, and the action queue takes the
few things on GitHub that wait on a person: a deploy held for approval, a
default branch failing, a serious alert, an admission about to lapse.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, NamedTuple

from django.utils.text import capfirst

from hq.domains.control_plane.observations.contract import ReadingPart
from hq.domains.control_plane.observations.github import (
    CODE_SCANNING,
    DEPENDABOT,
    LEAKED_CREDENTIALS,
    PROVIDER,
    PULL_REQUEST_KIND,
    REPOSITORY_KIND,
    WORKFLOW_RUN_KIND,
    CheckRun,
    JobFailure,
    PullRequestRecord,
    RepositoryRecord,
)
from hq.platform.application.labels import dotted

from .action_links import read_link
from .card_items import Badge, ListRow, RowTitle
from .derivations import whole
from .entity_links import web_url
from .expiry import days_until
from .freshness import Freshness, freshness
from .image_findings import SERIOUS
from .inbound_events import told
from .moments import duration, moment, when
from .projection import read_once
from .sequences import Step
from .thresholds import Threshold
from .tone import Tone
from .ui import severity_tone
from .workflow_contracts import ActionLink
lazy from . import asks
lazy from .app_updates import app_updates
lazy from .facts import record_read_at, snapshots_of
lazy from .images import GHCR, ImageRef
lazy from .projects import repository_key

# An admission artifact that lapses stops the composition admitting its
# extension, and nothing else says so until a deploy fails.
ARTIFACT_ATTENTION_DAYS = Threshold(14, "days")
ARTIFACT_SERIOUS_DAYS = Threshold(3, "days")
# A deploy held for approval is fine for a while; hours later it is a
# scheduled run about to be cancelled.
WAITING_SERIOUS_AFTER = Threshold(timedelta(hours=1), "time")
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
_CHECKS_TONES = {"failure": Tone.SERIOUS, "success": Tone.GOOD, "pending": Tone.ATTENTION}
# The tone of a deployment's state; any other state of one is still going.
_DEPLOY_TONES = {"success": Tone.GOOD, "failure": Tone.SERIOUS, "error": Tone.SERIOUS}
# The branches Renovate opens its pull requests from.
UPDATE_BRANCH = "renovate/"


def workflow_run(run: Mapping[str, Any]) -> ListRow:
    """One ``github.workflow_run`` record, as a row of a repository's panel:
    the workflow, what it came to (or where it is while it has not finished)
    in its tone, then its branch, commit and age."""

    conclusion, status = str(run.get("conclusion") or ""), str(run.get("status") or "")
    if conclusion == "success":
        tone = Tone.GOOD
    elif conclusion in ("failure", "timed_out"):
        tone = Tone.SERIOUS
    elif status != "completed":
        tone = Tone.ATTENTION
    else:
        tone = Tone.NEUTRAL
    state = (conclusion or status).replace("_", " ").capitalize()
    return ListRow(
        RowTitle(str(run.get("name") or "Workflow"), web_url(run.get("url")), external=True),
        meta=dotted(str(run.get("branch") or ""), str(run.get("sha") or "")[:7], _age(run.get("created_at"))),
        state=Badge(state, tone) if state else None,
    )


def _age(stamp: object) -> str:
    """How long ago a provider's stamp was, or nothing where it has none."""

    found = moment(str(stamp or ""))
    return when(found) if found else ""


def _pull_row(pull: Mapping[str, Any], *details: str, meta: str = "", state: Badge | None = None) -> ListRow:
    """A pull request as a row: its title leading to it on GitHub, its number first in the detail."""

    return ListRow(
        RowTitle(str(pull.get("title") or ""), web_url(pull.get("url")), external=True),
        detail=dotted(f"#{pull.get('number')}", *details),
        meta=meta,
        state=state,
    )


# A pull request's checks as its update row says them, by the combined state.
_PULL_CHECKS = {
    "failure": ("Checks failing", Tone.SERIOUS),
    "pending": ("Checks running", Tone.ATTENTION),
    "success": ("Checks passing", Tone.GOOD),
}


class PullChecks(NamedTuple):
    """A pull request's checks as every page says them: the badge, its tone,
    the failing ones named while they fail, and why, in the failing tool's
    own words: what its update run could not update, else its first failing
    job's cause (``failure_cause``)."""

    badge: str
    tone: Tone
    failing: str
    why: str


def pull_checks(pull: Mapping[str, Any]) -> PullChecks:
    checks = pull.get("checks") or {}
    badge, tone = _PULL_CHECKS.get(checks.get("state"), ("No checks", Tone.NEUTRAL))
    if checks.get("state") != "failure":
        return PullChecks(badge, tone, "", "")
    failures = checks.get("failures") or {}
    causes = (
        failure_cause({"log": pull.get("artifact_problem") or ()}),
        *(failure_cause(failures[job]) for job in checks.get("failing") or () if job in failures),
    )
    return PullChecks(badge, tone, ", ".join(checks.get("failing") or ()), next((each for each in causes if each), ""))


# A check's state in a word, by its conclusion once it completed, else its status.
_CONCLUDED = {"success": "passed", "failure": "failed", "cancelled": "cancelled", "timed_out": "timed out"}
_STATUS = {"queued": "queued", "in_progress": "running", "waiting": "waiting", "pending": "queued"}


class PullCheck(NamedTuple):
    """One check on a pull request's head, and whether GitHub told HQ of it
    since the pull request was read, which its next read replaces."""

    run: CheckRun
    told: bool = False


def check_step(check: PullCheck) -> Step:
    """One check of a pull request's head as a step every page draws: its
    name, ``Passed`` in ``58 s`` or ``Running`` since 2 minutes ago, told
    where an event said it since the read, linked to its run on GitHub where
    one is named."""

    run = check.run
    if run.status == "completed":
        # One that ended neither passed nor failed (skipped, neutral) stands idle.
        state = asks.DONE if run.conclusion == "success" else asks.FAILED if run.conclusion in FAILED else asks.IDLE
        word = _CONCLUDED.get(run.conclusion, run.conclusion)
    else:
        state = asks.RUNNING if run.status == "in_progress" else asks.QUEUED
        word = _STATUS.get(run.status, run.status)
    took = ""
    if run.started_at is not None:
        took = (
            str(duration(run.completed_at - run.started_at, short=True))
            if run.completed_at is not None
            else f"since {when(run.started_at)}"
        )
    standing = asks.Standing(state, took, word=capfirst(word), url=web_url(run.url))
    return Step(run.name, standing, by=PROVIDER, told=check.told)


# What GitHub's log marks a step's end with when the tool said nothing of
# its own: an exit code alone.
_EXITED = re.compile(r"(Process completed|The process '.*' failed) with exit code \d+\.?")
# A vulnerability govulncheck reports, by its id.
VULNERABILITY = re.compile(r"^Vulnerability #\d+: (\S+)")
# A line of a tool's output that says what failed.
_SAYS_FAILED = re.compile(r"\b(error|fatal|panic|failed|failure)\b", re.IGNORECASE)


def failure_cause(failure: Mapping[str, Any]) -> str:
    """Why a job failed, in its own words: the error GitHub's log marks,
    where it says more than an exit code; else the first line of output
    before it that says what failed, else the last; empty where the reading
    read no log (``JobFailure``)."""

    said = JobFailure.model_validate(failure)
    if said.error and not _EXITED.fullmatch(said.error):
        return said.error
    log = [line.strip() for line in said.log]
    return next(
        (line for line in log if _SAYS_FAILED.search(line) or VULNERABILITY.match(line)),
        log[-1] if log else said.error,
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
    # Its ``github.workflow_run`` records, newest first, and its
    # ``github.open_pull_request`` records, as the controller listed them.
    runs: tuple[Mapping[str, Any], ...] = ()
    pull_requests: tuple[Mapping[str, Any], ...] = ()

    def __getattr__(self, item: str) -> Any:
        """A field of the record, or the reading schema's default for it."""

        if item in self.record:
            return self.record[item]
        declared = RepositoryRecord.model_fields.get(item)
        if declared is None:
            raise AttributeError(item)
        return declared.get_default(call_default_factory=True)

    def pull_check_runs(self) -> dict[int, tuple[PullCheck, ...]]:
        """Each open pull request's checks by its number: each as it was read,
        with what a job's event said since that read laid over it as told,
        the one place a told check stands in for a read one."""

        said = told(PROVIDER, self.name, moment(str(self.record.get("read_at") or "")))
        found = {}
        for pull in self.pull_requests:
            checks = {run.name: PullCheck(run) for run in PullRequestRecord.model_validate(pull).check_runs}
            for each in said.get(str(pull.get("head") or ""), ()):
                if isinstance(each.fact, CheckRun):
                    checks[each.fact.name] = PullCheck(each.fact, told=True)
            found[int(pull.get("number") or 0)] = tuple(checks.values())
        return found

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
        """A link to each workflow whose newest run on the default branch failed."""

        found = []
        for run in self.runs:
            if run.get("branch") != self.default_branch or run.get("conclusion") not in FAILED:
                continue
            if url := web_url(run.get("url")):
                name = run.get("name") or "Workflow"
                found.append(read_link("failing-run", f"{name} run", url, reason=f"{name} failed on this branch."))
        return tuple(found)

    @property
    def workflow_runs(self) -> tuple[ListRow, ...]:
        """Each workflow's newest run on any branch, as the repository's panel shows it."""

        newest = {str(run.get("name") or ""): run for run in reversed(self.runs)}
        return tuple(workflow_run(run) for run in self.runs if newest[str(run.get("name") or "")] is run)

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
            return Tone.SERIOUS
        if self.open_alerts:
            return Tone.ATTENTION
        return Tone.GOOD if self.record.get("alerts") else ""

    @property
    def update_pulls(self) -> list[dict[str, Any]]:
        """Open pull requests Renovate opened, newest first, each with when it
        was opened (``opened``) and its age in whole days (``age_days``)."""

        found = []
        for pull in self.pull_requests:
            if str(pull.get("branch") or "").startswith(UPDATE_BRANCH):
                opened = moment(pull.get("created_at", ""))
                age = max(0, whole(opened, timedelta(days=1))) if opened else None
                found.append({**pull, "opened": opened, "age_days": age})
        return found

    @property
    def other_pulls(self) -> list[dict[str, Any]]:
        """Open pull requests a person or another tool opened."""

        return [pull for pull in self.pull_requests if not str(pull.get("branch") or "").startswith(UPDATE_BRANCH)]

    @property
    def pull_rows(self) -> tuple[ListRow, ...]:
        """``other_pulls`` as rows: each one's number, author and age, and
        whether it is a draft."""

        return tuple(
            _pull_row(
                pull,
                str(pull.get("author") or ""),
                meta=_age(pull.get("updated_at")),
                state=Badge("Draft") if pull.get("draft") else None,
            )
            for pull in self.other_pulls
        )

    @property
    def update_rows(self) -> tuple[ListRow, ...]:
        """The update pull requests, then the Dependabot alerts none of them
        answers, as one list: a pull request with how its checks stand (the
        failing ones named), an alert with its severity and what would fix it."""

        rows = []
        apps: dict[Any, list[str]] = {}
        for update in app_updates():
            if update.repository == self.name:
                apps.setdefault(update.number, []).extend((update.app, update.fixes_counted))
        for pull in self.update_pulls:
            checks = pull_checks(pull)
            opened = f"opened {when(pull['opened'])}" if pull["opened"] else ""
            said = apps.get(pull.get("number"), ())
            rows.append(
                _pull_row(pull, *said, checks.failing, checks.why, meta=opened, state=Badge(checks.badge, checks.tone))
            )
        for alert in self.unmatched_alerts:
            fixed = alert.get("first_patched_version")
            severity = str(alert.get("severity") or "")
            rows.append(
                ListRow(
                    RowTitle(str(alert.get("package") or ""), web_url(alert.get("url")), external=True),
                    detail=dotted(
                        "No update open",
                        f"fixed in {fixed}" if fixed else "no fixed version yet",
                        str(alert.get("manifest_path") or ""),
                    ),
                    state=Badge(severity, severity_tone(severity)) if severity else None,
                )
            )
        return tuple(rows)

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
                link = read_link(f"alerts-{part.name}", part.label, f"{self.url}/security/{page}")
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
    def verification_tip(self) -> str:
        """Each check production's last deploy ran before deploying, a line
        apiece with what it came to, as its verdict says on hover."""

        steps = (self.production or {}).get("verified") or []
        return "\n".join(f"{step.get('name')}: {step.get('conclusion') or 'not run'}" for step in steps)

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
        tone = _DEPLOY_TONES.get(self.production.get("state"), Tone.ATTENTION)
        # Live, with a check after the deploy failed: the controller reads it
        # from the outcome the deployment's status declares.
        return Tone.ATTENTION if tone == Tone.GOOD and self.production.get("check_failed") else tone

    @property
    def verified_tone(self) -> str:
        """``production_verified`` as a tone: none where the deploy ran no check."""

        return {True: Tone.GOOD, False: Tone.SERIOUS}.get(self.production_verified, "")

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

    def expiring(self, names: Collection[str]) -> list[dict[str, Any]]:
        """The artifacts called one of ``names`` that lapse within ``ARTIFACT_ATTENTION_DAYS``, soonest first.

        Each admission run uploads its own under one name, and Compose reads the
        newest run's, so a name lapses when the last of its artifacts does.
        """

        last: dict[str, tuple[Any, dict[str, Any]]] = {}
        for item in self.record.get("artifacts") or ():
            when = moment(item.get("expires_at", ""))
            name = item.get("name", "")
            if when is None or name not in names:
                continue
            if name not in last or last[name][0] < when:
                last[name] = (when, item)
        found = []
        for when, item in sorted(last.values(), key=lambda pair: pair[0]):
            days = days_until(when)
            if days <= ARTIFACT_ATTENTION_DAYS.value:
                found.append({**item, "days": max(days, 0)})
        return found


def repositories() -> dict[str, Repository]:
    """Every repository the App reads, by ``owner/name``. Read once per projection."""

    return read_once("github_estate.repositories", _load)


def _by_repository(kind: str) -> dict[str, list[Mapping[str, Any]]]:
    """A kind's records by the repository each names."""

    found: dict[str, list[Mapping[str, Any]]] = {}
    for snapshot in snapshots_of(kind):
        for record in snapshot.records or ():
            if isinstance(record, Mapping) and record.get("repository"):
                found.setdefault(str(record["repository"]), []).append(record)
    return found


def _newest_first(runs: list[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return tuple(sorted(runs, key=lambda run: str(run.get("created_at") or ""), reverse=True))


def _load() -> dict[str, Repository]:
    found: dict[str, Repository] = {}
    runs, pulls = _by_repository(WORKFLOW_RUN_KIND), _by_repository(PULL_REQUEST_KIND)
    for snapshot in snapshots_of(REPOSITORY_KIND):
        refused_by_repo: dict[str, list[str]] = {}
        for item in snapshot.refused_parts:
            if isinstance(item, Mapping):
                refused_by_repo.setdefault(str(item.get("scope", "")), []).append(str(item.get("part", "")))
        for record in snapshot.records or ():
            if isinstance(record, Mapping) and record.get("repository"):
                name = str(record["repository"])
                # A record carried across sweeps holds what it held when it
                # was read, so its age is that read's, never the sweep's.
                read_at = record_read_at(record) or snapshot.observed_at
                found[name] = Repository(
                    name,
                    record,
                    read_at,
                    tuple(refused_by_repo.get(name, ())),
                    _newest_first(runs.get(name, [])),
                    tuple(pulls.get(name, ())),
                )
    return found


def repository_for(url: str) -> Repository | None:
    """The repository a project's URL names, when the App reads it."""

    def load() -> dict[str, Repository]:
        return {repository_key(name): repo for name, repo in repositories().items()}

    key = repository_key(url)
    return read_once("github_estate.by_key", load).get(key) if key else None


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
