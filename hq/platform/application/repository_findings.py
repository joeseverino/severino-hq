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

from .derivations import reached, whole
from .entity_links import web_url
from .finding_model import Finding, FindingEstate, FindingRule, OperatorStep, opens
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
from .standards import Check, Standing
from .ui import counted, ended
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
    return replace(finding, title=was, severity="attention", notice=True)


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
            severity="serious" if started and reached(started + WAITING_SERIOUS_AFTER) else "attention",
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
        severity="serious",
        explanation=_named("Failing job", "Failing jobs", failing),
        evidence=(("Failing", str(len(failing) or 1)),),
        primary=opens("Open run", one.url) if one else opens("Open checks", f"{repo.url}/commit/{sha}"),
        offers=() if one else runs,
        # Where the reading does not hold the failing runs, the command that lists them.
        steps=()
        if runs or not sha
        else (
            OperatorStep(
                label="See the failing runs", command=f"gh run list -R {repo.name} --commit {sha} --status failure"
            ),
        ),
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
        severity="serious",
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
        severity="serious" if repo.leaked_credentials or repo.alerts_at("critical") else "attention",
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
            severity="serious" if days <= ARTIFACT_SERIOUS_DAYS else "attention",
            explanation="Once it lapses, HQ's next build leaves it out. Run its admission again to renew it.",
            evidence=(("Lapses in", counted(days, "day")),),
            primary=opens("Open actions", f"{repo.url}/actions"),
        )
        yield finding, f"{repo.short}'s admission was due to lapse in {days} days"


def _posture_steps(check: Check, missing: list[Repository]) -> tuple[tuple[OperatorStep, ...], str]:
    """Each repository's exact commands, or why there are none; beside
    commands, what they leave out is the last step, in words."""

    derive = COMMANDS.get(check.id)
    if derive is None:
        return (), REASONS[check.id]
    runs = tuple(OperatorStep(label=label, command=command) for repo in missing for label, command in derive(repo))
    called = tuple(
        OperatorStep(label=f"{repo.short}: check that {workflow} pins its own actions, then require pinning.")
        for repo in missing
        for workflow in (calls_workflows(repo) if check.id == "actions-pinned" else ())
    )
    said = left_out(check, missing)
    if not runs:
        return called, said
    return runs + called + ((OperatorStep(label=said),) if said else ()), ""


def _posture(_estate: FindingEstate) -> tuple[Finding, ...]:
    """One finding per check not met, naming every repository that misses it,
    so the queue grows with the gaps in the standard rather than with the estate."""

    found = postures()
    raised = []
    for check in STANDARD:
        unmet = [item.subject for item in found if item.state_of(check.id) == Standing.UNMET]
        if not unmet:
            continue
        missing = [repo.short for repo in unmet]
        steps, reason = _posture_steps(check, unmet)
        raised.append(
            Finding(
                rule=POSTURE,
                subject="",
                part=check.id,
                title=f"{check.label}: not met in {counted(len(missing), 'repository', 'repositories')}",
                severity="serious" if check.serious else "attention",
                explanation=f"{ended(', '.join(missing))} {check.fix}",
                evidence=(("Not met in", ", ".join(missing)), ("Why it matters", check.why)),
                primary=opens("Open repo checks", reverse("posture")),
                steps=steps,
                no_help_reason=reason,
                # A setting to change in each repository that misses it.
                count=len(missing),
                # The oldest of the readings it names: the claim is no newer.
                observed_at=min((repo.observed_at for repo in unmet if repo.observed_at), default=None),
                reads=REPOSITORY_KIND,
            )
        )
    return tuple(raised)


CANNOT_WRITE = "HQ reads GitHub with read-only tokens and never writes to a repository."

# The rules this module raises, beside the detectors that decide them.
RULES: tuple[FindingRule, ...] = (
    FindingRule(
        WAITING,
        "Deployments awaiting approval",
        "attention",
        _each(_waiting),
        operator_action="Approve or reject the run on GitHub, as one of its required reviewers.",
        no_help_reason="Only a required reviewer can approve a deployment, and HQ reads GitHub with read-only tokens.",
        bars={
            "serious": f"a run has waited on a reviewer for {duration(WAITING_SERIOUS_AFTER)}",
            "attention": "a run waits on a reviewer",
        },
        clears="a reviewer approves or rejects the run",
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        FAILING,
        "Failing workflows",
        "serious",
        _each(_failing),
        operator_action="Read the failing run's log on GitHub, then fix the code or the workflow it names.",
        no_help_reason="HQ reads which checks fail, not their logs, and the fix is a change to the code or a workflow.",
        bars={"serious": "a check fails on the newest commit to the default branch"},
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        UNVERIFIED,
        "Unverified commits",
        "serious",
        _each(_unverified),
        operator_action="Read the failed step in the deploy's log on GitHub, then fix the repository.",
        no_help_reason=(
            "HQ sees which step failed, not why. The cause is in the deploy's log, "
            "and the fix in the repository, which HQ never writes to."
        ),
        bars={
            "serious": "a step of the last deploy's own verification failed, so production may not be what was meant"
        },
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        ALERTS,
        "Security alerts",
        "attention",
        _each(_alerts),
        operator_action="Upgrade the code each alert names, or revoke the leaked credential, then let GitHub close it.",
        no_help_reason="An alert closes when the code is upgraded. HQ never writes to the repository.",
        bars={
            "serious": "a leaked credential or a critical alert is open, which anyone who finds it can use",
            "attention": "a high severity alert is open, which stays open to attack until the code is upgraded",
        },
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        LAPSING,
        "Workflow artifacts",
        "attention",
        _each(_lapsing),
        operator_action="Run the extension's admission workflow again on GitHub, which renews the artifact.",
        no_help_reason=(
            "The reading names the artifact but not the workflow that makes it, so HQ cannot name the run to repeat."
        ),
        bars={
            "serious": f"an admission artifact lapses within {counted(ARTIFACT_SERIOUS_DAYS, 'day')}",
            "attention": f"an admission artifact lapses within {counted(ARTIFACT_ATTENTION_DAYS, 'day')}",
        },
        clears="its admission runs again, which renews the artifact",
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        POSTURE,
        "Repository checks",
        "attention",
        _posture,
        operator_action="Change the setting the check names in each repository that misses it.",
        no_help_reason=CANNOT_WRITE,
        bars={
            "serious": "a repository misses a check HQ's standard holds serious",
            "attention": "a repository misses a check of HQ's standard",
        },
        domain=PROJECTS,
        gathered=True,
    ),
)
