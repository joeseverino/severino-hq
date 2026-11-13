"""One run through the real ansible-runner and ansible-playbook, on this machine
alone (``connection: local``), over the fake bridge: what ``mise run
smoke:runner`` runs, in an environment of the runner group alone, so it also
shows the worker needs nothing of HQ's own dependencies."""

import sys
import tempfile
from pathlib import Path

from hq_runner.ansible import runner
from hq_runner.claim import Procedure
from hq_runner.journal import Journal
from hq_runner.worker import work_once

from . import fakes

PROJECT = Path(__file__).with_name("smoke_project")


def main() -> int:
    answer = fakes.pending()
    answer["input"]["hosts"] = ["localhost"]
    bridge = fakes.Bridge(answer)
    installed = {fakes.PROCEDURE: Procedure(fakes.REVISION, PROJECT, "play.yml")}
    with tempfile.TemporaryDirectory(prefix="hq-runner-") as scratch:
        outcome = work_once(
            bridge,
            controller_id=fakes.WORKER,
            installed=installed,
            journal=Journal(Path(scratch) / "journal.json"),
            runner=runner(Path(scratch)),
        )
        left = sorted(path.name for path in Path(scratch).iterdir())
    report = outcome.report if outcome else {}
    seen = [(event["task"], event["outcome"]) for event in report.get("events", [])]
    print(f"smoke:runner: success {report.get('success')}, {report.get('message')}, events {seen}")
    wanted = [("Say which service", "ok"), ("Change something", "changed")]
    if report.get("success") is not True or seen != wanted:
        print(f"smoke:runner: wanted a success with events {wanted}", file=sys.stderr)
        return 1
    if left:
        print(f"smoke:runner: the run left {left} behind in its scratch directory", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
