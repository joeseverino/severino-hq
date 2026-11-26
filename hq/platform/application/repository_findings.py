"""Findings from what GitHub says of the repositories HQ is built from: what waits on a person there.

Read from the ``github.repository`` reading. One about an out-of-date reading
says what was read, in the past tense, as a notice only.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from dataclasses import replace
from datetime import timedelta
from typing import Any

from hq.domains.control_plane.observations.github import JobFailure
from hq.domains.control_plane.provider_adapters.github import COMMIT
from hq.platform.application.routes import reverse

from .action_links import read_link
from .deployment_review import APPROVE, REJECT, hq_rules, production_runs, review_target
from .derivations import reached, whole
from .entity_links import web_url
from .finding_model import Finding, FindingEstate, FindingRemedy, FindingRule, Notice, opens
from .finding_steps import Do, Run
from .github_estate import (
    ARTIFACT_ATTENTION_DAYS,
    ARTIFACT_SERIOUS_DAYS,
    FAILED,
    REPOSITORY_KIND,
    SERIOUS_ALERTS,
    WAITING_SERIOUS_AFTER,
    Repository,
    failure_cause,
    repositories,
)
from .github_posture import COMMANDS, REASONS, STANDARD, calls_workflows, left_out, postures
from .moments import moment
from .standard_findings import Help, Measured, Per, Standard
from .standards import Check
from .tone import Tone
from .ui import counted, ended
lazy from .capabilities import capability_words
lazy from .projects import projects_by_repository, repository_key

PROJECTS = "hq.projects"
WAITING = "github-waiting"
FAILING = "github-failing"
UNVERIFIED = "github-unverified"
ALERTS = "github-alerts"
LAPSING = "github-artifact"
POSTURE = "github-posture"
_COMMIT = re.compile(COMMIT)
# What releases a run HQ's answer has not reached, as GitHub's run page offers it.
BYPASS = "If HQ's answer has not reached GitHub, press Start all waiting jobs on the run's page there."

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
    # It tells of the reading it is drawn from; one with no time is the same unread reading until one arrives.
    return replace(finding, title=was, severity=Tone.ATTENTION, notice=Notice(repo.observed_at or "unread"))


def _each(said: Callable[[Repository], Iterator[Said]]) -> Callable[[FindingEstate], tuple[Finding, ...]]:
    def detect(_estate: FindingEstate) -> tuple[Finding, ...]:
        return tuple(
            _as_read(repo, found)
            for repo in sorted(repositories().values(), key=lambda item: item.name)
            for found in said(repo)
        )

    return detect


def _answers(repo: Repository, run: Mapping[str, Any]) -> tuple[FindingRemedy, ...]:
    """Approve and Reject for each environment the run waits on where HQ is
    a protection rule, at its commit: Reject alone for a commit production
    already runs, and none for a run whose commit the reading does not hold."""

    sha = str(run.get("sha") or "")
    if not _COMMIT.fullmatch(sha):
        return ()
    environments = [environment for environment in run.get("environments") or () if hq_rules(repo.name, environment)]
    answers = (REJECT,) if production_runs(repo.name, sha) else (APPROVE, REJECT)
    return tuple(
        FindingRemedy.posted(
            answer,
            review_target(repo.name, run, environment),
            intent=f"{capability_words(answer)} {environment}" if len(environments) > 1 else "",
        )
        for environment in environments
        for answer in answers
    )


def _waiting(repo: Repository) -> Iterator[Said]:
    for run in repo.waiting or ():
        started = moment(run.get("created_at", ""))
        hours = max(0, whole(started, timedelta(hours=1))) if started else 0
        where = ", ".join(run.get("environments") or ()) or "an environment"
        name = run.get("name") or "A run"
        sha = str(run.get("sha") or "")
        serious = bool(started) and reached(started + WAITING_SERIOUS_AFTER.value)
        finding = _about(
            repo,
            WAITING,
            str(run.get("id")),
            title=f"{name} waits for your approval to deploy to {where}",
            severity=Tone.SERIOUS if serious else Tone.ATTENTION,
            explanation=(
                f"Production already runs commit {sha[:7]} of {repo.short}"
                if production_runs(repo.name, sha)
                else f"Commit {sha[:7]} of {repo.short}"
            ),
            evidence=(("Waiting", f"{hours}h"),),
            primary=opens("Review deployment", web_url(run.get("url")) or repo.url),
            remedies=_answers(repo, run),
            # Long past its threshold, HQ's answer may not be reaching GitHub,
            # where an administrator can release the run without it.
            steps=(Do(BYPASS),) if serious else (),
        )
        yield finding, f"{name} was waiting for your approval to deploy to {where}"


def _failing(repo: Repository) -> Iterator[Said]:
    if not repo.failing:
        return
    checks = repo.record["checks"]
    # Each job by the step it failed at, and why, where the reading says.
    failures = checks.get("failures") or {}
    jobs = checks.get("failing") or []
    failing = [f"{job} (at {failures[job]['step']})" if failures.get(job, {}).get("step") else job for job in jobs]
    causes = [(job, cause) for job in jobs[:4] if (cause := failure_cause(failures.get(job) or {}))]
    said = [cause if len(jobs) == 1 else f"{job}: {cause}" for job, cause in causes]
    runs = repo.failing_runs
    one = runs[0] if len(runs) == 1 else None
    sha = repo.head.get("sha", "")
    # With no failing workflow run, what fails is a check on the head
    # commit that no workflow owns, and this lists it.
    listed = () if runs or not sha else (Run("See the failing checks", _failing_checks(repo.name, sha)),)
    finding = _about(
        repo,
        FAILING,
        title=f"{repo.short} is failing on {repo.default_branch}",
        severity=Tone.SERIOUS,
        explanation=". ".join((_named("Failing job", "Failing jobs", failing) or "A check failed.", *said)),
        evidence=(("Failing", str(len(failing) or 1)),),
        primary=opens("Open run", one.url) if one else opens("Open checks", f"{repo.url}/commit/{sha}"),
        offers=() if one else runs,
        steps=(*dict.fromkeys(step for job in jobs if (step := _next_step(failures.get(job) or {}))), *listed),
    )
    yield finding, f"{repo.short} was failing on {repo.default_branch}"


# A mise lock that holds no entry for a version its config pins, or that
# its config does not name: what locking again from the config answers.
_UNLOCKED = re.compile(r"mise ERROR .*(is not in the lockfile|cannot lock)")
# A vulnerability govulncheck reports, and the standard library release that fixes it.
_VULNERABILITY = re.compile(r"^Vulnerability #\d+: (\S+)")
_FIXED_IN_GO = re.compile(r"^Fixed in: (\S+)@(go\d\S*)")


def _next_step(failure: Mapping[str, Any]) -> Run | Do | None:
    """What to do about a job's failure in its own terms, where its log says
    which: none for any other, which the rule's step answers."""

    log = [line.strip() for line in JobFailure.model_validate(failure).log]
    if any(_UNLOCKED.search(line) for line in log):
        return Run("Lock every tool mise.toml pins, then commit mise.lock", "mise lock")
    vulnerability = next((found[1] for line in log if (found := _VULNERABILITY.match(line))), "")
    fixed = next((found for line in log if (found := _FIXED_IN_GO.match(line))), None)
    if fixed:
        return Do(
            ended(f"Move Go to {fixed[2]}, the release that fixes {vulnerability or fixed[1]} in {fixed[1]}"),
            value=fixed[2],
        )
    return None


def _failing_checks(name: str, sha: str) -> str:
    """The command that names each check failing on a commit, by the conclusions the reading counts."""

    failed = ", ".join(f'"{conclusion}"' for conclusion in sorted(FAILED))
    query = f".check_runs[] | select(.conclusion | IN({failed})) | .name"
    return f"gh api --paginate repos/{name}/commits/{sha}/check-runs --jq '{query}'"


def _unverified(repo: Repository) -> Iterator[Said]:
    if repo.production_verified is not False:
        return
    failed = [step["name"] for step in repo.production["verified"] if step.get("conclusion") != "success"]
    finding = _about(
        repo,
        UNVERIFIED,
        title=f"{repo.short}'s last deploy failed verification",
        severity=Tone.SERIOUS,
        explanation=_named("Failed step", "Failed steps", failed),
        evidence=(("Failed steps", str(len(failed))),),
        primary=opens("Open deploy", web_url(repo.production.get("url")) or repo.url),
    )
    yield finding, f"{repo.short}'s last deploy had failed verification"


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
        body += f" {counted(unmatched, 'has', 'have')} no update open."
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
            severity=Tone.SERIOUS if days <= ARTIFACT_SERIOUS_DAYS.value else Tone.ATTENTION,
            explanation="Run its admission again to keep it in the build.",
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
        Do(f"{repo.short}: check that this workflow pins its own actions, then require pinning.", value=workflow)
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
        no_help_reason="HQ answers only an environment where its deploy review App is the protection rule.",
        bars={
            Tone.SERIOUS: f"a run has waited for approval for {WAITING_SERIOUS_AFTER}",
            Tone.ATTENTION: "a run waits for approval",
        },
        clears="the run is approved or rejected",
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        FAILING,
        "Failing workflows",
        Tone.SERIOUS,
        _each(_failing),
        operator_action="Read the failing run's log on GitHub, then fix what it names.",
        no_help_reason=CANNOT_WRITE,
        bars={Tone.SERIOUS: "a workflow's latest run on the default branch fails, or a check on its newest commit"},
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        UNVERIFIED,
        "Unverified deploys",
        Tone.SERIOUS,
        _each(_unverified),
        operator_action="Read the failed step in the deploy's log on GitHub, then fix the repository.",
        no_help_reason="HQ sees which step failed, not why.",
        bars={Tone.SERIOUS: "a step of the last deploy's verification failed"},
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        ALERTS,
        "Security alerts",
        Tone.ATTENTION,
        _each(_alerts),
        operator_action="Upgrade what each alert names, or revoke the leaked credential.",
        no_help_reason=CANNOT_WRITE,
        bars={
            Tone.SERIOUS: "a leaked credential or a critical alert is open",
            Tone.ATTENTION: "a high severity alert is open",
        },
        domain=PROJECTS,
        gathered=True,
    ),
    FindingRule(
        LAPSING,
        "Lapsing admissions",
        Tone.ATTENTION,
        _each(_lapsing),
        operator_action="Run the extension's admission workflow again on GitHub.",
        no_help_reason="HQ cannot tell which workflow makes the artifact.",
        bars={
            Tone.SERIOUS: f"an admission artifact lapses within {ARTIFACT_SERIOUS_DAYS}",
            Tone.ATTENTION: f"an admission artifact lapses within {ARTIFACT_ATTENTION_DAYS}",
        },
        clears="its admission runs again, which renews the artifact",
        domain=PROJECTS,
        gathered=True,
    ),
    REPOSITORY_STANDARD.rule(domain=PROJECTS),
)
