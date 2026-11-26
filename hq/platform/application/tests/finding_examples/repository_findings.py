from typing import Any

from ...finding_steps import Do, Run
from ...github_posture import EXAMPLES as POSTURE
from ...tone import Tone
from . import NOW, Ago, Example, Examples, Raised, ago

KIND = "github.repository"
ALPHA = {
    "connection_ref": "github",
    "repository": "example/alpha",
    "url": "https://github.com/example/alpha",
    "default_branch": "main",
    "head": {"sha": "abc"},
}


def alpha(raises: tuple[Raised | str, ...] = (), **record) -> Example:
    """example/alpha, as its last read stated it, with ``record`` said of it."""

    return Example(readings={KIND: ({**ALPHA, **record},)}, raises=raises)


HEAD = "c" * 40


def waiting(since: Ago, sha: str = HEAD) -> list[dict]:
    return [{"id": 9, "name": "Compose", "environments": ["release"], "sha": sha, "created_at": since, "url": "u"}]


# The environment a run waits on, with HQ's deploy review App as its protection rule.
RULED = [{"name": "release", "hq_rule": True}]


def deployed(sha: str) -> list[dict]:
    """Production's newest deploy, at ``sha``."""

    return [{"environment": "production", "sha": sha, "state": "success", "url": "u"}]


def admission(*lapses: float) -> list[dict]:
    """The admission every run uploads under one name, lapsing in each of ``lapses`` days."""

    return [{"name": "admission-example.alpha", "expires_at": ago(days=-days, hours=-1)} for days in lapses]


def access(**changes) -> dict:
    """The access of the standard's kept repository, with ``changes``."""

    return {**POSTURE.kept["access"], **changes}


# A failing job as the reading says why: a lock that refuses an install, its
# cause above the exit GitHub marks, and a standard library fixed in a later
# release, its report many lines above it.
UNLOCKED = {
    "step": "Install tools",
    "error": "The process '/home/runner/.local/bin/mise' failed with exit code 1",
    "log": [
        "[command]/home/runner/.local/bin/mise install",
        "mise ERROR Failed to install aqua:astral-sh/uv@0.13.10: uv@0.13.10 is not in the lockfile",
        "mise ERROR Run with --verbose or MISE_VERBOSE=1 for more information",
    ],
}
VULNERABLE = {
    "step": "Scan",
    "error": "Process completed with exit code 3.",
    "log": [
        "=== Symbol Results ===",
        "Vulnerability #1: GO-2026-6859",
        "    A crafted request can exhaust a server's memory in net/http.",
        "  Standard library",
        "    Found in: net/http@go1.27.3",
        "    Fixed in: net/http@go1.27.4",
        "Your code is affected by 1 vulnerability from the Go standard library.",
    ],
}
# An error the tool marks itself says why on its own.
MARKED = {"step": "Lint", "error": "main.go:3:1: undefined: example", "log": ["golangci-lint run"]}


def failed(**failures: dict) -> dict[str, Any]:
    """The record of a workflow run whose jobs failed as ``failures`` say."""

    run = {"name": "CI", "url": "https://github.com/example/alpha/actions/runs/1"}
    return {"checks": {"state": "failure", "failing": sorted(failures), "failures": failures}, "failing_runs": [run]}


QUIET = alpha(checks={"state": "success"}, alerts={"code_scanning": {"low": 2}})
SERIOUS = Tone.SERIOUS
WAITS = "Compose waits for your approval to deploy to release"
ANSWERS = ("deployment.approve", "deployment.reject")
BYPASS = Do("If HQ's answer has not reached GitHub, press Start all waiting jobs on the run's page there.")

EXAMPLES = (
    Examples(
        "github-waiting",
        # Held for hours, it is serious, and says how to release it without HQ.
        fires=(
            alpha(
                waiting=waiting(ago(hours=3)),
                environments=RULED,
                raises=(Raised(title=WAITS, severity=SERIOUS, presses=ANSWERS, steps=(BYPASS,)),),
            ),
            alpha(waiting=waiting(ago(minutes=5)), environments=RULED, raises=(Raised(title=WAITS, presses=ANSWERS),)),
            # A commit production already runs is only offered Reject.
            alpha(
                waiting=waiting(ago(minutes=5)),
                environments=RULED,
                deployments=deployed(HEAD),
                raises=(
                    Raised(
                        title=WAITS,
                        explanation="Production already runs commit ccccccc of alpha",
                        presses=("deployment.reject",),
                    ),
                ),
            ),
            # A run whose commit the reading does not hold, or held where HQ is
            # not a protection rule, is answered on GitHub alone.
            alpha(waiting=waiting(ago(minutes=5), sha=""), environments=RULED, raises=(WAITS,)),
            alpha(waiting=waiting(ago(minutes=5)), raises=(WAITS,)),
        ),
        quiet=(QUIET,),
    ),
    Examples(
        "github-failing",
        fires=(
            alpha(checks={"state": "failure", "failing": ["lint"]}, raises=("alpha is failing on main",)),
            # A job of a workflow run says the step it failed at, and why where
            # its log was read.
            alpha(
                checks={"state": "failure", "failing": ["lint", "test"], "failures": {"test": {"step": "Run tests"}}},
                raises=(
                    Raised(title="alpha is failing on main", explanation="Failing jobs: lint, test (at Run tests)"),
                ),
            ),
            alpha(
                **failed(ci=UNLOCKED),
                raises=(
                    Raised(
                        title="alpha is failing on main",
                        explanation="Failing job: ci (at Install tools). mise ERROR Failed to install"
                        " aqua:astral-sh/uv@0.13.10: uv@0.13.10 is not in the lockfile",
                        steps=(Run("Lock every tool mise.toml pins, then commit mise.lock", "mise lock"),),
                    ),
                ),
            ),
            alpha(
                **failed(scan=VULNERABLE, lint=MARKED),
                raises=(
                    Raised(
                        title="alpha is failing on main",
                        explanation="Failing jobs: lint (at Lint), scan (at Scan). lint: main.go:3:1: undefined:"
                        " example. scan: Vulnerability #1: GO-2026-6859",
                        steps=(
                            Do(
                                "Move Go to go1.27.4, the release that fixes GO-2026-6859 in net/http.",
                                value="go1.27.4",
                            ),
                        ),
                    ),
                ),
            ),
        ),
        quiet=(QUIET,),
    ),
    Examples(
        "github-unverified",
        fires=(
            alpha(
                deployments=[
                    {
                        "environment": "production",
                        "url": "u",
                        "verified": [{"name": "Verify the image was signed", "conclusion": "failure"}],
                    }
                ],
                raises=("alpha's last deploy failed verification",),
            ),
        ),
        quiet=(QUIET,),
    ),
    Examples(
        "github-alerts",
        fires=(
            alpha(
                alerts={"code_scanning": {"critical": 1, "low": 4}},
                raises=(Raised(title="1 serious alert in alpha", severity=SERIOUS),),
            ),
            alpha(
                alerts={"leaked_credentials": {"leaked": 1}},
                raises=(Raised(title="1 leaked credential in alpha", severity=SERIOUS),),
            ),
        ),
        quiet=(QUIET, alpha(alerts={"leaked_credentials": {"leaked": 0}, "dependabot": {}})),
    ),
    Examples(
        "github-artifact",
        # An admission uploaded by two runs lapses with the newer.
        fires=(alpha(artifacts=admission(2, 6), raises=("alpha's admission lapses in 6 days",)),),
        quiet=(QUIET,),
    ),
    Examples(
        "github-posture",
        # An archived repository is held to nothing.
        fires=(
            Example(
                readings={
                    KIND: tuple(
                        {
                            **ALPHA,
                            "repository": f"example/{name}",
                            "url": f"https://github.com/example/{name}",
                            "private": True,
                            "access": access(vulnerability_alerts=False),
                            "variables": [],
                            "archived": name == "retired",
                        }
                        for name in ("kept", "retired")
                    )
                },
                raises=(Raised(title="Vulnerability alerts are off", evidence=(("Not met in", "kept"),)),),
            ),
        ),
        quiet=(alpha(private=True, access=access(), variables=[], read_at=NOW),),
    ),
)
