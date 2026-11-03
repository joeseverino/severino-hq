"""The releases production has run, as GitHub's deployments record them.

``previous COMMIT`` prints the commit production ran before COMMIT, or nothing
when no other release was live: the Deploy workflow's compare link, and the
base `mise run release:report` measures against. Stdlib only: the job that
runs it sets up no toolchain.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

# The record the Deploy workflow writes of each release (.github/workflows/deploy.yml).
DEPLOYMENTS = "repos/{owner}/{repo}/deployments?environment=production&per_page=100"
# What each status the workflow writes means, as it writes them.
OUTCOMES = Path(__file__).resolve().parents[2] / "deploy" / "outcomes.json"

Statuses = Callable[[int], list[dict[str, Any]]]


@cache
def declared() -> tuple[dict[str, Any], ...]:
    return tuple(json.loads(OUTCOMES.read_text(encoding="utf-8"))["outcomes"].values())


def outcome(status: dict[str, Any]) -> dict[str, Any] | None:
    """The declared outcome a deployment status reports, by its state and description
    together: None for a status the workflow did not write from the declaration."""

    return next(
        (
            each
            for each in declared()
            if each["state"] == status.get("state") and each["description"] == status.get("description")
        ),
        None,
    )


def gh(path: str) -> Any:
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=60, check=False)
    if done.returncode:
        raise OSError(f"gh api {path.partition('?')[0]} exited {done.returncode}: {done.stderr.strip()}")
    return json.loads(done.stdout)


def statuses(ident: int) -> list[dict[str, Any]]:
    return gh(f"repos/{{owner}}/{{repo}}/deployments/{ident}/statuses")


@dataclass(frozen=True)
class Releases:
    previous: str | None
    # Whether the release itself was ever marked live, and the commit production runs now.
    deployed: bool
    running: str | None
    # Whether the release went live with a check after its deploy failed.
    check_failed: bool = False
    # The deploy whose rollback failed since production last went live, which
    # leaves what it runs unknown: running is None then.
    unknown_since: str | None = None


def releases(release: str, deployments: list[dict[str, Any]], statuses: Statuses) -> Releases:
    """The release production ran before ``release``: the newest deployment marked live before
    ``release``'s own, or, for a release never deployed, the newest marked live. A deployment's
    statuses keep its "success" after a newer release marks it inactive."""

    running = None
    deployed = False
    check_failed = False
    unknown_since = None
    for deployment in sorted(deployments, key=lambda each: each["created_at"], reverse=True):
        kept = statuses(deployment["id"])
        live = next((status for status in kept if status["state"] == "success"), None)
        if live is None:
            ended = outcome(kept[0]) if kept else None
            if running is None and unknown_since is None and ended and ended["production"] == "unknown":
                unknown_since = deployment["sha"]
            continue
        running = running or (None if unknown_since else deployment["sha"])
        if deployment["sha"] == release:
            # Its newest run to go live says how it stands.
            check_failed = check_failed if deployed else bool((outcome(live) or {}).get("check_failed"))
            deployed = True
            continue
        return Releases(deployment["sha"], deployed, running, check_failed, unknown_since)
    return Releases(None, deployed, running, check_failed, unknown_since)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m tools.pipeline.production")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("previous", help="the commit production ran before COMMIT").add_argument("commit")
    args = parser.parse_args(argv)
    try:
        found = releases(args.commit, gh(DEPLOYMENTS), statuses)
    except (OSError, ValueError, subprocess.TimeoutExpired) as failed:
        print(f"GitHub's deployments were not read: {failed}", file=sys.stderr)
        return 1
    if found.previous:
        print(found.previous)
    return 0


if __name__ == "__main__":
    sys.exit(main())
