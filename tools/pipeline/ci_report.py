"""Where a change stands, said on its commit as HQ's app.

    python3 -m tools.pipeline.ci_report check NAME SHA STATUS TITLE SUMMARY [CONCLUSION]
    python3 -m tools.pipeline.ci_report verdict
    python3 -m tools.pipeline.ci_report report
    python3 -m tools.pipeline.ci_report failed-step JOB_NAME
    gh run view RUN --log-failed | python3 -m tools.pipeline.ci_report diagnose

``check`` opens or moves one of HQ's checks. ``verdict`` is CI's: ready, or
held with why and what fixes it. ``report`` is Deploy's: live, or not
deployed and why, plus one comment on the merged pull request. ``failed-step``
names the steps that failed in one job of this run. ``diagnose`` prints the
catalog entry a log matches, as JSON.

It runs on the hosted runner's own ``python3`` with the standard library and
``gh`` only: the jobs that post a check carry no toolchain step, so a review
opens the moment CI starts. ``pyproject.toml`` holds it to that interpreter.

Everything it posts on this public repository is a job name, GitHub's own
record of the run or the catalog's text (``deploy/diagnoses.json``). A log is
read only to be matched against the catalog, never repeated.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

REVIEW = "Severino HQ · Review"
PRODUCTION = "Severino HQ · Production"
# HQ's controller writes the same marker on an extension's pull request.
DELIVERY_MARK = "<!-- severino-hq-delivery:{} -->"

STATUSES = ("queued", "in_progress", "completed")
CONCLUSIONS = ("success", "failure", "neutral", "cancelled", "skipped", "timed_out", "action_required")

CATALOG = Path(__file__).resolve().parents[2] / "deploy" / "diagnoses.json"
UNKNOWN = {
    "id": "unknown",
    "title": "A step failed",
    "fix": "No known cause matched. The failing step's log says what happened; once it is understood, add it to deploy/diagnoses.json so it is recognized next time.",
}

# GitHub echoes each step's script and environment between these markers, so
# every message a step can print is in its log whether or not it ran.
ECHOED = re.compile(r"^[^\n]*##\[group\]Run .*?^[^\n]*##\[endgroup\][^\n]*$", re.MULTILINE | re.DOTALL)


def diagnoses() -> list[dict[str, str]]:
    entries: list[dict[str, str]] = json.loads(CATALOG.read_text())["diagnoses"]
    return entries


def diagnose(log: str) -> dict[str, str]:
    """The first catalog entry the log matches, in the catalog's words only."""
    printed = ECHOED.sub("", log)
    for entry in diagnoses():
        if re.search(entry["match"], printed):
            return {key: entry[key] for key in ("id", "title", "fix")}
    return dict(UNKNOWN)


# GitHub ------------------------------------------------------------------

Runner = Callable[[Sequence[str], Mapping[str, str], "str | None"], str]


def _gh(args: Sequence[str], env: Mapping[str, str], stdin: str | None) -> str:
    done = subprocess.run(["gh", *args], env=dict(env), input=stdin, capture_output=True, text=True, check=False)
    if done.returncode != 0:
        raise GitHubError(
            done.stderr.strip().splitlines()[0] if done.stderr.strip() else f"gh exited {done.returncode}"
        )
    return done.stdout


class GitHubError(RuntimeError):
    """A call GitHub refused, by gh's first line of error."""


# A run's jobs outgrow one page with a matrix, and the jobs on the next page
# are the ones a verdict would never see, so a listing is read only by pages().
_JOB_LISTING = re.compile(r"/jobs(\?|$)")


class GitHub:
    """GitHub's REST API through gh, as one token, or as gh's own login when given none."""

    def __init__(self, token: str | None, repo: str, run: Runner = _gh) -> None:
        self.token = token
        self.repo = repo
        self._run = run

    def _call(self, *args: str, body: object = None) -> str:
        env = {**os.environ, **({"GH_TOKEN": self.token} if self.token else {})}
        return self._run(["api", *args], env, None if body is None else json.dumps(body))

    def get(self, path: str) -> Any:
        if _JOB_LISTING.search(path):
            raise ValueError(f"{path} lists jobs: read it with pages()")
        return json.loads(self._call(path))

    def pages(self, path: str, key: str | None = None) -> list[Any]:
        """Every item of a listing, from every page."""
        pages = json.loads(self._call("--paginate", "--slurp", path))
        return [item for page in pages for item in (page[key] if key else page)]

    def post(self, path: str, body: object) -> None:
        self._call("-X", "POST", path, "--input", "-", body=body)

    def patch(self, path: str, body: object) -> None:
        self._call("-X", "PATCH", path, "--input", "-", body=body)


def _quietly[T](read: Callable[[], T], default: T) -> T:
    """A detail GitHub may not have yet: its absence changes no verdict."""
    try:
        return read()
    except (GitHubError, LookupError, ValueError):
        return default


@dataclass(frozen=True)
class Job:
    id: int
    name: str
    conclusion: str
    seconds: int | None
    url: str
    steps: tuple[Mapping[str, Any], ...] = ()

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> Job:
        started, completed = raw.get("started_at"), raw.get("completed_at")
        seconds = None
        if started and completed:
            seconds = int((_moment(completed) - _moment(started)).total_seconds())
        return cls(
            id=int(raw["id"]),
            name=str(raw["name"]),
            conclusion=raw.get("conclusion") or "pending",
            seconds=seconds,
            url=raw.get("html_url") or "",
            steps=tuple(raw.get("steps") or ()),
        )


def _moment(text: str) -> datetime:
    # GitHub's one shape of instant; HQ's own parser is not importable here.
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")


def jobs(gh: GitHub, run: str, attempt: str | None = None) -> list[Job]:
    """Every job of a run, or of one attempt of it."""
    scope = f"runs/{run}/attempts/{attempt}" if attempt else f"runs/{run}"
    return [Job.of(raw) for raw in gh.pages(f"repos/{gh.repo}/actions/{scope}/jobs?per_page=100", "jobs")]


def failed_steps(gh: GitHub, run: str, attempt: str, job_name: str) -> list[str]:
    """The steps that failed in one job; a called workflow's job is "Caller / JOB"."""
    return [
        step["name"]
        for job in jobs(gh, run, attempt)
        if job.name == job_name or job.name.endswith(f" / {job_name}")
        for step in job.steps
        if step.get("conclusion") == "failure"
    ]


class LogUnreadable(RuntimeError):
    """A job log the API did not serve, by its status line alone."""


_TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})


def _backoff(attempt: int, retry_after: str | None) -> float:
    """How long to wait before the next attempt: what the server asked for, or doubling from a second."""
    if retry_after and retry_after.strip().isdigit():
        return float(retry_after)
    return float(2 ** (attempt - 1))


def job_log(
    job: int,
    *,
    token: str,
    repo: str,
    api: str = "https://api.github.com",
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """One job's log, read only to be matched against the catalog.

    Not through gh, which refuses a response holding terminal escapes, and a
    log is full of them. The token goes to the API only: the log is served
    from a storage host the API redirects to, and an unredirected header is
    not carried there.
    """
    request = urllib.request.Request(
        f"{api}/repos/{repo}/actions/jobs/{int(job)}/logs", headers={"Accept": "application/vnd.github+json"}
    )
    request.add_unredirected_header("Authorization", f"Bearer {token}")
    for attempt in range(1, attempts + 1):
        retry_after = None
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body: bytes = response.read()
                return body.decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            error.close()
            if error.code not in _TRANSIENT or attempt == attempts:
                raise LogUnreadable(f"HTTP {error.code} {error.reason}") from None
            retry_after = error.headers.get("Retry-After")
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt == attempts:
                raise LogUnreadable(f"not reached: {getattr(error, 'reason', error)}") from None
        sleep(_backoff(attempt, retry_after))
    raise AssertionError("unreachable")


# Checks ------------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    conclusion: str
    title: str
    summary: str


def post_check(
    gh: GitHub, name: str, sha: str, status: str, verdict: Verdict, *, text: str = "", url: str = ""
) -> None:
    """Move the check still in progress on the commit, or open one beside a finished one.

    A finished check is the record of its run; a redeploy of the same commit
    opens a new one, which GitHub then shows.
    """
    listing = f"repos/{gh.repo}/commits/{sha}/check-runs?check_name={quote(name, safe='')}&filter=latest"
    open_ = [run for run in gh.pages(listing, "check_runs") if run.get("status") != "completed"]
    output = {"title": verdict.title, "summary": verdict.summary} | ({"text": text} if text else {})
    body: dict[str, object] = {"name": name, "head_sha": sha, "status": status, "output": output}
    if verdict.conclusion:
        body["conclusion"] = verdict.conclusion
    if url:
        body["details_url"] = url
    if open_:
        gh.patch(f"repos/{gh.repo}/check-runs/{open_[0]['id']}", body)
    else:
        gh.post(f"repos/{gh.repo}/check-runs", body)


def took(seconds: int) -> str:
    return f"{seconds // 60}m {seconds % 60}s" if seconds >= 60 else f"{seconds}s"


def read_logs(failed: Iterable[Job], read: Callable[[int], str], warn: Callable[[str], None]) -> str:
    """The logs of what failed; one that cannot be read is named, by its status line."""
    logs = []
    for job in failed:
        try:
            logs.append(read(job.id))
        except LogUnreadable as error:
            warn(f"::warning title=Log not read::{job.name}: {error}")
    return "\n".join(logs)


# The verdict -------------------------------------------------------------

# The gate's own jobs and the push-only proof are not gates of a pull request.
NOT_GATES = frozenset({"Ready", "Release", "Proven on its pull request"})
# In the order the pipeline runs; the first prefix that matches.
ORDER: tuple[tuple[str, int], ...] = (
    ("Checks", 1),
    ("Tests", 2),
    ("Browser", 3),
    ("Controller", 4),
    ("CodeQL", 5),
    ("Image checks", 7),
    ("Image", 6),
    ("Composition", 8),
)
MARKS = {
    "success": "✓ Passed",
    "skipped": "– Skipped",
    "failure": "✗ Failed",
    "cancelled": "✗ Cancelled",
    "timed_out": "✗ Timed out",
}


def unmet(results: Mapping[str, Mapping[str, str]], required: Sequence[str]) -> list[str]:
    """A needed job that did not pass; skipped passes unless the job is required."""
    lines = [
        f"{job}: {need['result']}"
        for job, need in results.items()
        if need["result"] != "success" and (need["result"] != "skipped" or job in required)
    ]
    return lines + [f"{job}: not run" for job in required if job not in results]


def _rank(name: str) -> int:
    return next((rank for prefix, rank in ORDER if name.startswith(prefix)), 9)


def gate_table(gates: Sequence[Job]) -> str:
    rows = [
        (
            _rank(job.name),
            job.name,
            f"| {f'[{job.name}]({job.url})' if job.url else job.name} | {MARKS.get(job.conclusion, '✗ ' + job.conclusion)}"
            f" | {'' if job.seconds is None else took(job.seconds)} |",
        )
        for job in gates
    ]
    lines = [row for _, _, row in sorted(rows)]
    return "| Gate | Result | Took |\n| --- | --- | ---: |\n" + "\n".join(lines) + "\n"


def held(gates: Sequence[Job], unmet_lines: Sequence[str], diagnosis: Mapping[str, str]) -> Verdict:
    # A required job that was skipped has no failed row of its own, so it is
    # named as it was needed.
    names = [job.name for job in gates if job.conclusion not in ("success", "skipped")]
    names = names or [line.split(":", 1)[0] for line in unmet_lines]
    return Verdict("failure", f"Held: {diagnosis['title']}", f"Not passing: {', '.join(names)}. {diagnosis['fix']}")


@dataclass(frozen=True)
class Run:
    """This run, as the workflow names it."""

    id: str
    url: str


def review(
    reader: GitHub, app: GitHub, run: Run, head_sha: str, unmet_lines: Sequence[str], read: Callable[[int], str]
) -> Verdict:
    """HQ's review on the pull request, posted as its check."""
    gates = [job for job in jobs(reader, run.id) if job.name not in NOT_GATES]
    if not unmet_lines:
        verdict = Verdict("success", "Ready to merge", "Every gate passed.")
    else:
        broken = [job for job in gates if job.conclusion not in ("success", "skipped")]
        verdict = held(gates, unmet_lines, diagnose(read_logs(broken, read, _warn)))
    post_check(app, REVIEW, head_sha, "completed", verdict, text=gate_table(gates), url=run.url)
    return verdict


# The production report ---------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    commit: str
    image: str
    result: str
    run: Run
    compose_run: str = ""
    server: str = "https://github.com"


def _approvals(gh: GitHub, run: str) -> list[Mapping[str, Any]]:
    return _quietly(lambda: gh.pages(f"repos/{gh.repo}/actions/runs/{run}/approvals"), [])


def _deploy_job(gh: GitHub, run: str) -> Job | None:
    return _quietly(lambda: next((job for job in jobs(gh, run) if job.name == "Deploy"), None), None)


def _deploy_log(reader: GitHub, run: str, read: Callable[[int], str], wait: Callable[[], None]) -> str:
    # A finished job's log can take a few seconds to be served.
    job = _deploy_job(reader, run)
    if job is None:
        return ""
    for _ in range(6):
        with contextlib.suppress(LogUnreadable):
            if log := read(job.id):
                return log
        wait()
    return ""


def outcome(reader: GitHub, delivery: Delivery, read: Callable[[int], str], wait: Callable[[], None]) -> Verdict:
    if delivery.result == "success":
        return Verdict("success", "Live in production", "Production runs this commit, and it passed its health check.")
    if delivery.result == "cancelled":
        return Verdict(
            "cancelled",
            "Not deployed: cancelled",
            "The deploy was cancelled. Production still runs the previous image.",
        )
    # Not approved, or approved and failed. GitHub records which: asked, not
    # inferred from a log that may simply not be downloadable yet.
    if not any(entry.get("state") == "approved" for entry in _approvals(reader, delivery.run.id)):
        return Verdict(
            "neutral",
            "Not deployed: not approved",
            "The deploy did not run. Production still runs the previous image. Run Deploy again to deploy this commit.",
        )
    diagnosis = diagnose(_deploy_log(reader, delivery.run.id, read, wait))
    return Verdict("failure", f"Not deployed: {diagnosis['title']}", diagnosis["fix"])


def _ci_url(gh: GitHub, commit: str) -> str:
    runs: list[Any] = _quietly(
        lambda: gh.pages(f"repos/{gh.repo}/actions/runs?head_sha={commit}&per_page=100", "workflow_runs"), []
    )
    return next((str(run["html_url"]) for run in runs if run.get("name") == "CI"), "")


def _pipeline(gh: GitHub, delivery: Delivery) -> str:
    pipeline = f"[Deploy]({delivery.run.url})"
    if delivery.compose_run.isdigit():
        pipeline = f"[Compose]({delivery.server}/{gh.repo}/actions/runs/{delivery.compose_run}) → {pipeline}"
    ci = _ci_url(gh, delivery.commit)
    return f"[CI]({ci}) → {pipeline}" if ci else pipeline


def details(gh: GitHub, delivery: Delivery, verdict: Verdict) -> str:
    """The delivery from GitHub's own record of it: every value is already public here."""
    commit = delivery.commit
    headline = _quietly(
        lambda: str(gh.get(f"repos/{gh.repo}/commits/{commit}")["commit"]["message"]).splitlines()[0], ""
    )
    approver = next((entry for entry in _approvals(gh, delivery.run.id) if entry.get("state") == "approved"), None)
    approval = f"@{approver['user']['login']}" if approver else "not recorded"
    if verdict.conclusion == "success":
        job = _deploy_job(gh, delivery.run.id)
        result = "Healthy" + (f" in {took(job.seconds)}" if job and job.seconds is not None else "")
    else:
        result = verdict.title.removeprefix("Not deployed: ")
    name, digest = delivery.image.rsplit("@", 1)[0], delivery.image.rsplit("@", 1)[-1]
    return (
        "| | |\n| --- | --- |\n"
        f"| **Change** | [`{commit[:7]}`]({delivery.server}/{gh.repo}/commit/{commit}) {headline} |\n"
        f"| **Image** | `{name.rsplit('/', 1)[-1]}@{digest[:19]}…` |\n"
        f"| **Approved** | {approval} |\n"
        f"| **Deploy** | {result} |\n"
        f"| **Pipeline** | {_pipeline(gh, delivery)} |\n\n"
        "<details><summary>Full image reference</summary>\n\n"
        f"```\n{delivery.image}\n```\n\n</details>"
    )


def comment_once(app: GitHub, delivery: Delivery) -> None:
    """One comment on the merged pull request, the first time its commit is live."""
    pulls = [pull for pull in app.pages(f"repos/{app.repo}/commits/{delivery.commit}/pulls") if pull.get("merged_at")]
    if not pulls:
        return
    issue = f"repos/{app.repo}/issues/{pulls[0]['number']}/comments"
    marker = DELIVERY_MARK.format(delivery.commit)
    if any(marker in (comment.get("body") or "") for comment in app.pages(f"{issue}?per_page=100")):
        return
    app.post(issue, {"body": f"{marker}\n**Live in production.** `{delivery.image}` passed its health check."})


def report(
    reader: GitHub, app: GitHub, delivery: Delivery, read: Callable[[int], str], wait: Callable[[], None]
) -> Verdict:
    """Where the deploy ended up, on its commit's Production check."""
    verdict = outcome(reader, delivery, read, wait)
    text = details(reader, delivery, verdict)
    post_check(app, PRODUCTION, delivery.commit, "completed", verdict, text=text, url=delivery.run.url)
    if verdict.conclusion == "success":
        comment_once(app, delivery)
    return verdict


# The command line --------------------------------------------------------


def _warn(line: str) -> None:
    print(line, file=sys.stderr)


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"ci_report: {name} must be set")
    return value


def _reader_of(token: str) -> tuple[GitHub, Callable[[int], str]]:
    repo = _env("GITHUB_REPOSITORY")
    api = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    return GitHub(token, repo), lambda job: job_log(job, token=token, repo=repo, api=api)


def _run_check(args: argparse.Namespace) -> int:
    gh = GitHub(_env("GH_TOKEN"), _env("GITHUB_REPOSITORY"))
    verdict = Verdict(args.conclusion or "", args.title, args.summary)
    post_check(
        gh,
        args.name,
        args.sha,
        args.status,
        verdict,
        text=os.environ.get("CHECK_TEXT", ""),
        url=os.environ.get("CHECK_URL", ""),
    )
    return 0


def _run_verdict(_: argparse.Namespace) -> int:
    lines = unmet(json.loads(_env("RESULTS")), os.environ.get("REQUIRED", "").split())
    app_token = os.environ.get("APP_TOKEN", "")
    if not app_token:
        if lines:
            print("\n".join(lines))
            print("::error::Not ready: every job above must pass.")
            return 1
        print("Every gate passed.")
        return 0
    reader, read = _reader_of(_env("GH_TOKEN"))
    app = GitHub(app_token, reader.repo)
    verdict = review(reader, app, Run(_env("THIS_RUN"), _env("RUN_URL")), _env("HEAD_SHA"), lines, read)
    print(verdict.title)
    return 0 if verdict.conclusion == "success" else 1


def _run_report(_: argparse.Namespace) -> int:
    reader, read = _reader_of(_env("GH_TOKEN"))
    delivery = Delivery(
        commit=_env("COMMIT"),
        image=_env("IMAGE"),
        result=_env("RESULT"),
        run=Run(_env("THIS_RUN"), _env("RUN_URL")),
        compose_run=os.environ.get("COMPOSE_RUN", ""),
        server=os.environ.get("GITHUB_SERVER_URL") or "https://github.com",
    )
    verdict = report(reader, GitHub(_env("APP_TOKEN"), reader.repo), delivery, read, lambda: time.sleep(5))
    print(verdict.title)
    return 0


def _run_failed_step(args: argparse.Namespace) -> int:
    gh = GitHub(_env("GH_TOKEN"), _env("GITHUB_REPOSITORY"))
    steps = failed_steps(gh, _env("GITHUB_RUN_ID"), os.environ.get("GITHUB_RUN_ATTEMPT") or "1", args.job)
    for step in steps:
        print(step)
    return 0


def _run_diagnose(_: argparse.Namespace) -> int:
    print(json.dumps(diagnose(sys.stdin.read())))
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="python3 -m tools.pipeline.ci_report", description="Where a change stands.")
    commands = root.add_subparsers(required=True, dest="command")
    check = commands.add_parser("check", help="open or move one of HQ's checks (GH_TOKEN, CHECK_TEXT, CHECK_URL)")
    check.add_argument("name")
    check.add_argument("sha")
    check.add_argument("status", choices=STATUSES)
    check.add_argument("title")
    check.add_argument("summary")
    check.add_argument("conclusion", nargs="?", choices=CONCLUSIONS)
    check.set_defaults(run=_run_check)
    commands.add_parser("verdict", help="CI's verdict, from RESULTS and REQUIRED").set_defaults(run=_run_verdict)
    commands.add_parser("report", help="Deploy's report on its commit").set_defaults(run=_run_report)
    step = commands.add_parser("failed-step", help="the steps that failed in one job of this run")
    step.add_argument("job")
    step.set_defaults(run=_run_failed_step)
    commands.add_parser("diagnose", help="the catalog entry a log on stdin matches").set_defaults(run=_run_diagnose)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
