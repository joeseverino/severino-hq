"""The one command whose exit 0 means ready to release.

Runs, in order:

1. ``mise run ci``: every gate the pipeline runs (one that cannot run fails);
   or, with --remote, what GitHub ran on this exact commit, waited for and
   required to pass: CI's newest run, which concludes success only when its
   verdict job (Ready, or Release on main) passed, on main the Compose it
   starts, and every other run and check. The commit must be pushed.
2. ``mise run suite:as-image``, the extensions required.
3. ``hq-host check`` on the deploy host, read-only, as the SSH login: built
   here for the host's platform from HEAD, so the release being checked is
   the one that checks, and handed the units this release and main ship.

The host is SEVERINO_HQ_DEPLOY_HOST, an SSH destination, set in the
environment or in mise.local.toml (tools/dev/mise.local.example.toml). The
release is HEAD, which must be committed: a check of uncommitted files proves
nothing about the commit that ships.

Exit 0: ready. Exit 1: a gate failed. Exit 2: the host was skipped, or its
release could not be verified, so whatever else passed, this is not a verdict
on the release.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tools.gate.verdict import Step, close, run

ROOT = Path(__file__).resolve().parents[2]

CI = ".github/workflows/ci.yml"
COMPOSE = ".github/workflows/compose.yml"
DEPLOY = ".github/workflows/deploy.yml"
# Deploy waits for an approval this gate comes before.
PASSING = {"success", "skipped", "neutral"}
# What `uname -m` says, as Go names it.
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
# hq-host check's exits.
UNVERIFIED = 3
# Watches that fail in a row before the watch, not the run, is the fault.
WATCH_FAILURES = 5


@dataclass
class Summary:
    rows: list[tuple[str, Step]] = field(default_factory=list)

    def add(self, status: str, step: Step) -> None:
        step.ok = status == "passed"
        self.rows.append((status, step))

    def command(self, label: str, cmd: list[str]) -> None:
        print(f"[preflight] {label}", flush=True)
        step = run(label, cmd)
        self.add("passed" if step.ok else "FAILED", step)

    def check(self, label: str, gate) -> None:
        print(f"[preflight] {label}", flush=True)
        self.add("passed" if gate() else "FAILED", Step(label))


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()


def gh(*args: str) -> str:
    return subprocess.run(["gh", *args], cwd=ROOT, check=True, capture_output=True, text=True).stdout


class API:
    """GitHub's REST API, read through gh as its own login."""

    def __init__(self, call: Callable[[list[str]], str] = lambda args: gh(*args)):
        self._call = call

    def pages(self, path: str, key: str | None = None) -> list:
        """Every item of a listing, from every page."""
        pages = json.loads(self._call(["api", "--paginate", "--slurp", path]))
        return [item for page in pages for item in (page[key] if key else page)]


class GitHub:
    """What GitHub ran on one commit, read until all of it has finished."""

    def __init__(self, sha: str, repository: str):
        self.sha, self.repository = sha, repository
        self.api = API()
        self.runs: list[dict] = []
        self.checks: list[dict] = []

    def read(self) -> None:
        newest: dict[str, dict] = {}
        for item in self.api.pages(
            f"repos/{self.repository}/actions/runs?head_sha={self.sha}&per_page=100", "workflow_runs"
        ):
            item["path"] = item["path"].split("@", 1)[0]
            if item["path"] != DEPLOY and item["created_at"] > newest.get(item["path"], {}).get("created_at", ""):
                newest[item["path"]] = item
        self.runs = sorted(newest.values(), key=lambda item: item["path"])
        self.checks = self.api.pages(
            f"repos/{self.repository}/commits/{self.sha}/check-runs?filter=latest&per_page=100", "check_runs"
        )

    def ci(self) -> dict | None:
        return next((item for item in self.runs if item["path"] == CI), None)

    def unsettled(self) -> tuple[list[str], int | None]:
        """What is still missing or running, and a run to watch."""

        waiting = []
        ci = self.ci()
        if ci is None:
            waiting.append("no CI run on this commit")
        elif ci["event"] == "push" and not any(item["path"] == COMPOSE for item in self.runs):
            waiting.append("CI's Release has not started Compose")
        running = [item for item in self.runs if item["status"] != "completed"]
        waiting += [f"{item['path']} is {item['status']}" for item in running]
        waiting += [
            f"check {item['name']} is {item['status']}" for item in self.checks if item["status"] != "completed"
        ]
        return waiting, (running[0]["id"] if running else None)

    def settle(self, deadline: float, poll: int) -> bool:
        failures = 0
        while True:
            self.read()
            waiting, running = self.unsettled()
            if not waiting:
                return True
            if time.monotonic() >= deadline:
                print("GitHub did not finish with this commit in time:", file=sys.stderr)
                for line in waiting:
                    print(f"  {line}", file=sys.stderr)
                return False
            if running is None:
                time.sleep(poll)
                continue
            error = self.watch(running, poll)
            if error is None:
                failures = 0
                continue
            failures += 1
            if failures >= WATCH_FAILURES:
                print(f"gh run watch failed {failures} times in a row: {error}", file=sys.stderr)
                return False
            time.sleep(poll)

    def watch(self, run: int, poll: int) -> str | None:
        """Wait for one run to finish; what gh said when it could not."""

        watched = subprocess.run(
            ["gh", "run", "watch", str(run), "-R", self.repository, "--interval", str(poll)],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if watched.returncode == 0:
            return None
        lines = watched.stderr.strip().splitlines()
        return lines[0] if lines else f"gh exited {watched.returncode}"

    def passed(self) -> bool:
        ok = True
        for item in self.runs:
            print(f"  {item.get('conclusion') or '-'}  {item['path']} ({item['event']}, run {item['id']})")
        for item in self.checks:
            print(f"  {item.get('conclusion') or '-'}  {item['name']}")
        if any(
            item.get("conclusion") != "success"
            if item["path"] in (CI, COMPOSE)
            else item.get("conclusion") not in PASSING
            for item in self.runs
        ):
            print("  a workflow run did not pass")
            ok = False
        if any(item.get("conclusion") not in PASSING for item in self.checks):
            print("  a check did not pass")
            ok = False
        return ok


def github_gates() -> bool:
    sha = git("rev-parse", "HEAD")
    if not git("branch", "-r", "--contains", sha):
        print("HEAD is not pushed, so GitHub has checked nothing of it.", file=sys.stderr)
        return False
    repository = gh("repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner").strip()
    github = GitHub(sha, repository)
    deadline = time.monotonic() + int(os.environ.get("PREFLIGHT_REMOTE_TIMEOUT", "3600"))
    try:
        return github.settle(deadline, int(os.environ.get("PREFLIGHT_REMOTE_POLL", "30"))) and github.passed()
    except subprocess.CalledProcessError as error:
        print(f"GitHub could not be read: {error.stderr or error}", file=sys.stderr)
        return False


def ssh(destination: str, *command: str, **options) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", "-o", "BatchMode=yes", destination, *command], cwd=ROOT, **options)


def build_check(work: Path, platform: tuple[str, str]) -> Path:
    """hq-host as HEAD has it, built for the host's platform."""

    source = work / "source"
    source.mkdir()
    archive = subprocess.run(["git", "archive", "HEAD", "controller"], cwd=ROOT, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(source)], input=archive.stdout, check=True)
    program = work / "payload" / "hq-host"
    program.parent.mkdir()
    goos, goarch = platform
    subprocess.run(
        ["go", "build", "-trimpath", "-o", str(program), "./cmd/hq-host"],
        cwd=source / "controller",
        env={**os.environ, "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"},
        check=True,
    )
    return program


def unit_trees(payload: Path) -> None:
    """The units this release and main ship, as release/ and main/."""

    for name, ref in (("release", "HEAD"), ("main", os.environ.get("PREFLIGHT_BASE_REF", "origin/main"))):
        into = payload / name
        into.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", ref, "deploy/systemd"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)


# Run by the login shell: unpack, check, remove, exit as the check did.
REMOTE = (
    'dir="$(mktemp -d "${TMPDIR:-/tmp}/hq-preflight.XXXXXX")" || exit 1; '
    'if tar -x -C "$dir"; then "$dir/hq-host" check --release "$dir/release/deploy/systemd" '
    '--main "$dir/main/deploy/systemd"; status=$?; else status=1; fi; rm -rf "$dir"; exit "$status"'
)


def host_check(destination: str) -> int:
    reported = ssh(destination, "uname", "-s", "-m", capture_output=True, text=True)
    system, _, machine = reported.stdout.strip().partition(" ")
    if reported.returncode != 0 or system != "Linux" or machine not in ARCHITECTURES:
        print(f"The deploy host could not be reached, or runs {reported.stdout.strip() or 'nothing it names'}.")
        return 1
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        try:
            build_check(work, ("linux", ARCHITECTURES[machine]))
            unit_trees(work / "payload")
        except subprocess.CalledProcessError as error:
            print(f"The check could not be built from HEAD: {shlex.join(map(str, error.cmd))} failed.")
            return 1
        # ssh's stderr goes to a file, so it cannot fill a pipe while the payload is written.
        with (
            tempfile.TemporaryFile() as said,
            subprocess.Popen(
                ["ssh", "-o", "BatchMode=yes", destination, REMOTE], cwd=ROOT, stdin=subprocess.PIPE, stderr=said
            ) as proc,
        ):
            assert proc.stdin
            sent = send(proc.stdin, work / "payload")
            status = proc.wait()
            said.seek(0)
            stderr = said.read().decode("utf-8", "replace")
        sys.stderr.write(stderr)
        if not sent:
            print(
                f"The deploy host stopped reading the check (ssh exited {status}): {stderr.strip() or 'it said nothing'}"
            )
            return 1
        return status


def send(stdin, payload: Path) -> bool:
    """Stream the payload as a tar; False when the host stopped reading first."""

    try:
        with stdin, tarfile.open(fileobj=stdin, mode="w|") as archive:
            archive.add(payload, arcname=".")
    except BrokenPipeError:
        return False
    return True


def main() -> int:
    started = time.monotonic()
    summary = Summary()
    head = git("rev-parse", "--short", "HEAD")
    if git("status", "--porcelain"):
        summary.add("FAILED", Step("committed tree (uncommitted changes are not part of the release)"))
    else:
        summary.add("passed", Step(f"committed tree at {head}"))

    if os.environ.get("usage_remote"):  # noqa: SIM112  mise names a flag usage_<flag>
        summary.check("every GitHub check on this commit", github_gates)
    else:
        summary.command("every gate the pipeline runs", ["mise", "run", "-c", "ci"])
    summary.command(
        "the composed suite, as the image runs it", ["env", "REQUIRE_COMPOSED=1", "mise", "run", "suite:as-image"]
    )

    destination = os.environ.get("SEVERINO_HQ_DEPLOY_HOST", "")
    if os.environ.get("usage_skip_host"):  # noqa: SIM112  mise names a flag usage_<flag>
        summary.add("SKIPPED", Step("deploy host (--skip-host)"))
    elif not destination:
        summary.add("FAILED", Step("deploy host: SEVERINO_HQ_DEPLOY_HOST is not set"))
    else:
        label = f"deploy host {destination}, read-only"
        print(f"[preflight] {label}", flush=True)
        status = host_check(destination)
        if status == UNVERIFIED:
            summary.add("UNKNOWN", Step(f"{label}: the release it runs is unverified"))
        else:
            summary.add("passed" if status == 0 else "FAILED", Step(label))

    print("\n[preflight] summary")
    for status, step in summary.rows:
        print(f"  {status:<8} {step.label}")
    failed = [step for status, step in summary.rows if status == "FAILED"]
    if failed:
        close("preflight", started, failed, failed=": NOT READY")
        return 1
    if any(status != "passed" for status, _ in summary.rows):
        print("[preflight] HOST NOT VERIFIED. Every other gate passed; this is not a release verdict.")
        return 2
    close("preflight", started, [step for _, step in summary.rows], passed=": READY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
