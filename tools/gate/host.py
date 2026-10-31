"""The check:host task: the deploy host's half of `mise run check --release`,
read-only. hq-host check, built from HEAD for the host's platform, runs there
over SSH with the units this release and main ship; its exit is this one's.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# What `uname -m` says, as Go names it.
ARCHITECTURES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


def ssh(destination: str, *command: str, **options) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", destination, *command], cwd=ROOT, check=options.pop("check", False), **options
    )


def build_check(work: Path, platform: tuple[str, str]) -> Path:
    """hq-host as HEAD has it, built for the host's platform, so the release checked is the one that checks."""

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

    for name, ref in (("release", "HEAD"), ("main", os.environ.get("CHECK_RELEASE_BASE_REF", "origin/main"))):
        into = payload / name
        into.mkdir(parents=True)
        archive = subprocess.run(["git", "archive", ref, "deploy/systemd"], cwd=ROOT, check=True, capture_output=True)
        subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)


# Run by the login shell: unpack, check, remove, exit as the check did.
REMOTE = (
    'dir="$(mktemp -d "${TMPDIR:-/tmp}/hq-check.XXXXXX")" || exit 1; '
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
    destination = os.environ.get("SEVERINO_HQ_DEPLOY_HOST", "")
    if not destination:
        print("SEVERINO_HQ_DEPLOY_HOST is not set: it names the deploy host's SSH destination.")
        return 1
    return host_check(destination)


if __name__ == "__main__":
    sys.exit(main())
