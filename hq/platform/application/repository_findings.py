"""Findings from what GitHub says of the repositories HQ is built from: what waits on a person there.

A deploy held for approval, a default branch failing, a deploy that failed its
own verification, a serious alert, an admission about to lapse, and each check
of HQ's repository standard some repository misses. Read from the
``github.repository`` reading (``github_estate``) and raised under Projects.
Once a repository's reading is out of date, a finding about it says what was
read, in the past tense, and only as a notice, so an old reading never stands
as a current fact.
"""

from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import timedelta

from hq.platform.application.routes import reverse

from .action_links import read_link
from .derivations import reached, whole
from .entity_links import web_url
from .finding_model import Finding, FindingEstate, FindingRule, opens
from .finding_steps import Do, Run
from .github_estate import (
    ARTIFACT_ATTENTION_DAYS,
    ARTIFACT_SERIOUS_DAYS,
    REPOSITORY_KIND,
    SERIOUS_ALERTS,
    WAITING_SERIOUS_AFTER,
    Repository,
    repositories,
)
from .github_posture import COMMANDS, REASONS, STANDARD, calls_workflows, left_out, postures
from .moments import duration, moment
from .standard_findings import Help, Measured, Per, Standard
from .standards import Check
from .tone import Tone
from .ui import counted
lazy from .projects import projects_by_repository, repository_key

PROJECTS = "hq.projects"
WAITING = "github-waiting"
FAILING = "github-failing"
UNVERIFIED = "github-unverified"
ALERTS = "github-alerts"
LAPSING = "github-artifact"
POSTURE = "github-posture"

# Each finding about one repository, with what it says once its reading is out of date.
Said = tuple[Finding, str]


def _named(one: str, many: str, names: list[str]) -> str:
    """Names under what they are ("Failing job: Scan"), so a line of bare
    names never leaves a reader guessing; the first four, then a count."""

    shown = ", ".join(names[:4]) + (f" and {len(names) - 4} more" if len(names) > 4 else "")
    return f"{one if len(names) == 1 else many}: {shown}" if names else ""


def _about(repo: Repository, rule: str, part: str = "", **fields) -> Finding:
    return Finding(
        rule=rule,
        subject="",
        part=f"{repo.name}:{part}" if part else repo.name,
        observed_at=repo.observed_at,
        reads=REPOSITORY_KIND,
        **fields,
    )


def _as_read(repo: Repository, found: Said) -> Finding:
    """A finding as of the reading it is drawn from."""

    finding, was = found
    if not repo.reading.stale:
        return finding
    return replace(finding, title=was, severity=Tone.ATTENTION, notice=True)


def _each(said: Callable[[Repository], Iterator[Said]]) -> Callable[[FindingEstate], tuple[Finding, ...]]:
    def detect(_estate: FindingEstate) -> tuple[Finding, ...]:
        return tuple(
            _as_read(repo, found)
            for repo in sorted(repositories().values(), key=lambda item: item.name)
            for found in said(repo)
        )

    return detect


def _waiting(repo: Repository) -> Iterator[Said]:
    for run in repo.waiting or ():
        started = moment(run.get("created_at", ""))
        hours = max(0, whole(started, timedelta(hours=1))) if started else 0
        where = ", ".join(run.get("environments") or ()) or "an environment"
        name = run.get("name") or "A run"
        finding = _about(
            repo,
            WAITING,
            str(run.get("id")),
            title=f"{name} waits for your approval to deploy to {where}",
            severity=Tone.SERIOUS if started and reached(started + WAITING_SERIOUS_AFTER) else Tone.ATTENTION,
            explanation=f"Commit {run.get('sha', '')[:7]} of {repo.short}",
            evidence=(("Waiting", f"{hours}h"),),
            primary=opens("Review deployment", web_url(run.get("url")) or repo.url),
        )
        yield finding, f"{name} was waiting for your approval to deploy to {where}"


def _failing(repo: Repository) -> Iterator[Said]:
    if not repo.failing:
        return
    failing = repo.record["checks"].get("failing") or []
    runs = repo.failing_runs
    one = runs[0] if len(runs) == 1 else None
    sha = repo.head.get("sha", "")
    finding = _about(
        repo,
        FAILING,
        title=f"{repo.short} is failing on {repo.default_branch}",
        severity=Tone.SERIOUS,
        explanation=_named("Failing job", "Failing jobs", failing),
        evidence=(("Failing", str(len(failing) or 1)),),
        primary=opens("Open run", one.url) if one else opens("Open checks", f"{repo.url}/commit/{sha}"),
        offers=() if one else runs,
        # Where the reading does not hold the failing runs, the command that lists them.
        steps=()
        if runs or not sha
        else (Run("See the failing runs", f"gh run list -R {repo.name} --commit {sha} --status failure"),),
    )
    yield finding, f"{repo.short} was failing on {repo.default_branch}"


def _unverified(repo: Repository) -> Iterator[Said]:
    if repo.production_verified is not False:
        return
    failed = [step["name"] for step in repo.production["verified"] if step.get("conclusion") != "success"]
    finding = _about(
        repo,
        UNVERIFIED,
        title=f"{repo.short}'s last deploy did not pass its own verification",
        severity=Tone.SERIOUS,
        explanation=_named("Failed step", "Failed steps", failed),
        evidence=(("Failed steps", str(len(failed))),),
        primary=opens("Open deploy", web_url(repo.production.get("url")) or repo.url),
    )
    yield finding, f"{repo.short}'s last deploy had not passed its own verification"


def _alerts_said(repo: Repository) -> tuple[str, str, str]:
    """What the alert finding counts, as one and as many, and what kind they are."""

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


def _alerts(repo: Repository) -> Iterator[Said]:
    kinds = repo.alert_links(SERIOUS_ALERTS)
    if not kinds:
        return
    one, many, body = _alerts_said(repo)
    unmatched = sum(1 for alert in repo.unmatched_alerts if alert.get("severity") in SERIOUS_ALERTS)
    unfixed = sum(
        1
        for alert in repo.record.get("dependabot_alerts") or ()
        if alert.get("severity") in SERIOUS_ALERTS and not alert.get("first_patched_version")
    )
    updates = _updates_url(repo) if unmatched else ""
    if unmatched:
        body += f" {counted(unmatched, 'has', 'have')} no update pull request."
    finding = _about(
        repo,
        ALERTS,
        title=f"{counted(repo.serious_alerts, one, many)} in {repo.short}",
        severity=Tone.SERIOUS if repo.leaked_credentials or repo.alerts_at("critical") else Tone.ATTENTION,
        explanation=body,
        evidence=(("Open", str(repo.serious_alerts)),),
        primary=opens("Open updates", updates) if updates else opens("Open alerts", kinds[0].url),
        offers=kinds if updates or len(kinds) > 1 else (),
        # Every one a Dependabot alert no version fixes yet.
        awaiting_fix=unfixed == repo.serious_alerts,
    )
    yield finding, f"{counted(repo.serious_alerts, f'{one} was', f'{many} were')} open in {repo.short}"


def _lapsing(repo: Repository) -> Iterator[Said]:
    for artifact in repo.expiring():
        days = artifact["days"]
        finding = _about(
            repo,
            LAPSING,
            artifact["name"],
            title=f"{repo.short}'s admission lapses in {days} days",
            severity=Tone.SERIOUS if days <= ARTIFACT_SERIOUS_DAYS else Tone.ATTENTION,
            explanation="Once it lapses, HQ's next build leaves it out. Run its admission again to renew it.",
            evidence=(("Lapses in", counted(days, "day")),),
            primary=opens("Open actions", f"{repo.url}/actions"),
        )
        yield finding, f"{repo.short}'s admission was due to lapse in {days} days"


def _posture_help(check: Check, subjects: tuple[Measured, ...]) -> Help:
    """Each repository's exact commands, or why there are none; beside
    commands, what they leave out is the last step, in words."""

    missing = [item.measurement.subject for item in subjects]
    settings = tuple(read_link("open", repo.short, f"{repo.url}/settings") for repo in missing)
    derive = COMMANDS.get(check.id)
    if derive is None:
        return Help(no_help_reason=REASONS[check.id], offers=settings)
    runs = tuple(Run(label, command) for repo in missing for label, command in derive(repo))
    called = tuple(
        Do(f"{repo.short}: check that {workflow} pins its own actions, then require pinning.")
        for repo in missing
        for workflow in (calls_workflows(repo) if check.id == "actions-pinned" else ())
    )
    said = left_out(check, missing)
    if not runs:
        return Help(steps=called, no_help_reason=said, offers=settings)
    return Help(steps=runs + called + ((Do(said),) if said else ()), offers=settings)


def repositories_measured(_estate: FindingEstate | None = None) -> tuple[Measured, ...]:
    """Each repository HQ is built from, measured against the repository standard."""

    return tuple(
        Measured(
            measurement=found, name=found.subject.short, observed_at=found.subject.observed_at, reads=REPOSITORY_KIND
        )
        for found in postures()
    )


CANNOT_WRITE = "HQ reads GitHub with read-only tokens and never writes to a repository."

REPOSITORY_STANDARD = Standard(
    name=POSTURE,
    title="Repository checks",
    severity=Tone.ATTENTION,
    checks=STANDARD,
    subjects=repositories_measured,
    raises=Per.CHECK,
    operator_action="Change the setting the check names in each repository that misses it.",
    no_help_reason=CANNOT_WRITE,
    bars={
        Tone.SERIOUS: "a repository misses a check HQ's standard holds serious",
        Tone.ATTENTION: "a repository misses a check of HQ's standard",
    },
    primary=lambda: opens("Open repo checks", reverse("posture")),
    helps=_posture_help,
)

# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        WAITING,
        "Deployments awaiting approval",
        Tone.ATTENTION,
        _each(_waiting),
        operator_action="Approve or reject the run on GitHub, as one of its required reviewers.",
        no_help_reason="Only a required reviewer can approve a deployment, and HQ reads GitHub with read-only tokens.",
        bars={
            Tone.SERIOUS: f"a run has waited on a reviewer for {duration(WAITING_SERIOUS_AFTER)}",
            Tone.ATTENTION: "a run waits on a reviewer",
        },
        clears="a reviewer approves or rejects the run",
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        FAILING,
        "Failing workflows",
        Tone.SERIOUS,
        _each(_failing),
        operator_action="Read the failing run's log on GitHub, then fix the code or the workflow it names.",
        no_help_reason="HQ reads which checks fail, not their logs, and the fix is a change to the code or a workflow.",
        bars={Tone.SERIOUS: "a check fails on the newest commit to the default branch"},
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        UNVERIFIED,
        "Unverified commits",
        Tone.SERIOUS,
        _each(_unverified),
        operator_action="Read the failed step in the deploy's log on GitHub, then fix the repository.",
        no_help_reason=(
            "HQ sees which step failed, not why. The cause is in the deploy's log, "
            "and the fix in the repository, which HQ never writes to."
        ),
        bars={
            Tone.SERIOUS: "a step of the last deploy's own verification failed, so production may not be what was meant"
        },
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        ALERTS,
        "Security alerts",
        Tone.ATTENTION,
        _each(_alerts),
        operator_action="Upgrade the code each alert names, or revoke the leaked credential, then let GitHub close it.",
        no_help_reason="An alert closes when the code is upgraded. HQ never writes to the repository.",
        bars={
            Tone.SERIOUS: "a leaked credential or a critical alert is open, which anyone who finds it can use",
            Tone.ATTENTION: "a high severity alert is open, which stays open to attack until the code is upgraded",
        },
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        LAPSING,
        "Workflow artifacts",
        Tone.ATTENTION,
        _each(_lapsing),
        operator_action="Run the extension's admission workflow again on GitHub, which renews the artifact.",
        no_help_reason=(
            "The reading names the artifact but not the workflow that makes it, so HQ cannot name the run to repeat."
        ),
        bars={
            Tone.SERIOUS: f"an admission artifact lapses within {counted(ARTIFACT_SERIOUS_DAYS, 'day')}",
            Tone.ATTENTION: f"an admission artifact lapses within {counted(ARTIFACT_ATTENTION_DAYS, 'day')}",
        },
        clears="its admission runs again, which renews the artifact",
        domain=PROJECTS,
        gathered=True,
    ),
    REPOSITORY_STANDARD.rule(domain=PROJECTS),
)
