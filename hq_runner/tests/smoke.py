"""One run through the worker's own entry point, the real ansible-runner and
ansible-playbook, on this machine alone, over the fake bridge: what ``mise run
smoke:runner`` runs, in an environment of the runner group alone, so it also
shows the worker needs nothing of HQ's own dependencies.

The procedure is fetched at its commit from a repository on this machine
(``remote``), and its machine is reached through a runner connection in a
connections document as the renderer writes one; the smoke playbook alone
sets its connection to local, so no ssh is opened.
"""

import dataclasses
import sys
import tempfile
from pathlib import Path

from hq_runner.__main__ import Config, drain

from . import fakes
from .remote import REPOSITORY, Remote

PROJECT = Path(__file__).with_name("smoke_project")


def _environment(root: Path) -> dict[str, str]:
    connections = root / "connections.json"
    fakes.write_connections(connections, fakes.connection("localhost", host="127.0.0.1", role="runner"))
    for directory in ("scratch", "state", "ssh"):
        (root / directory).mkdir()
    return {
        "HQ_AUTOMATION_ID": fakes.WORKER,
        "HQ_AUTOMATION_BRIDGE_SOCKET": str(root / "bridge" / "bridge.sock"),
        "HQ_AUTOMATION_JOURNAL": str(root / "state" / "journal.json"),
        "HQ_AUTOMATION_CONNECTIONS": str(connections),
        "HQ_AUTOMATION_SSH_DIR": str(root / "ssh"),
        "HQ_AUTOMATION_SCRATCH": str(root / "scratch"),
        "HQ_AUTOMATION_REPOSITORY": REPOSITORY,
        "HQ_AUTOMATION_PROJECT_DIR": "ansible",
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hq-runner-") as directory:
        root = Path(directory)
        remote = Remote(root / "remote")
        files = {f"ansible/{path.name}": path.read_text(encoding="utf-8") for path in PROJECT.iterdir()}
        revision = remote.commit(files)
        answer = fakes.pending(revision=revision)
        answer["resource"]["spec"]["playbook"] = "play.yml"
        answer["input"]["hosts"] = ["localhost"]
        bridge = fakes.Bridge(answer)
        config = Config.read(_environment(root))
        # The one change from the configuration the environment gives: the
        # repository is this machine's, reached over git's file transport.
        config = dataclasses.replace(config, project=dataclasses.replace(config.project, origin=remote.origin))
        worked = drain(config, bridge)
        left = sorted(path.name for path in (root / "scratch").iterdir())
    report = bridge.reports[0] if bridge.reports else {}
    seen = [(event["task"], event["outcome"]) for event in report.get("events", [])]
    print(f"smoke:runner: {worked} runs, success {report.get('success')}, {report.get('message')}, events {seen}")
    wanted = [("Say which service", "ok"), ("Change something", "changed")]
    if worked != 1 or report.get("success") is not True or seen != wanted:
        print(f"smoke:runner: wanted one success with events {wanted}", file=sys.stderr)
        return 1
    if left:
        print(f"smoke:runner: the run left {left} behind in its scratch directory", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
