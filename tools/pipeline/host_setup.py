"""The host's setup playbook (deploy/ansible/host.yml) run for real by the
runner group's ansible-playbook against a host made of directories, with
systemd, visudo and the preflights answered by fakes that keep what they are
asked: what ``mise run smoke:host-setup`` runs.

It holds the playbook's promises: what a deploy's restart needs (`--tags web`)
is every drop-in, the credential mount, the renderer and HQ, and acts on
nothing; a fresh apply installs every shipped file, renders host.json's values
as they are (a template among them stays text), makes the tmpfiles rule's
directories and then starts the renderer, before the preflights, and every unit with an [Install]
section but no other; a second apply changes no file and a check after it
nothing; a check finds an edited unit, an edited drop-in and a unit the
release no longer ships, which an apply then stops and removes; and a rule
visudo refuses is never installed. Each run's wall time is printed, the
connection (local, no pipelining) forking each module as nsenter's does.

``--record PATH`` writes the drifted check's report there, its fixture paths
read as a host's, for controller/host's tests of what verify reads of it.

Stdlib only, at the runner group's Python.
"""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOK = ROOT / "deploy" / "ansible" / "host.yml"
# A template in a host.json value, which must be written as text.
VAULT = "{{ 7 * 7 }} Vault"

FAKE = r'''#!{python}
"""A fake of {name}: each call appended to calls.log, systemctl's state kept."""
import json, os, sys
from pathlib import Path
state = Path({state!r})
args = sys.argv[1:]
with open(state / "calls.log", "a") as log:
    log.write(" ".join([{name!r}] + args) + "\n")
if {name!r} == "visudo":
    sys.exit(1 if (state / "visudo-refuses").exists() else 0)
if {name!r} != "systemctl":
    sys.exit(0)
units = Path({systemd!r})
book = state / "systemd.json"
held = json.loads(book.read_text()) if book.exists() else {{"enabled": [], "active": []}}
words = [arg for arg in args if not arg.startswith("-")]
verb, unit = words[0], (words[1] if len(words) > 1 else "")
path = units / unit
def installs():
    return path.is_file() and "[Install]" in path.read_text().splitlines()
def file_state():
    if not installs():
        return "static"
    return "enabled" if unit in held["enabled"] else "disabled"
if verb == "show":
    if not path.is_file():
        print("Id=" + unit + "\nLoadState=not-found\nActiveState=inactive")
    else:
        active = "active" if unit in held["active"] else "inactive"
        print(f"Id={{unit}}\nLoadState=loaded\nActiveState={{active}}\nSubState=dead\nUnitFileState={{file_state()}}")
elif verb == "is-enabled":
    print(file_state())
    sys.exit(0 if file_state() in ("enabled", "static") else 1)
elif verb == "is-active":
    sys.exit(0 if unit in held["active"] else 3)
elif verb in ("enable", "disable"):
    held["enabled"] = sorted(set(held["enabled"]) - {{unit}} | ({{unit}} if verb == "enable" else set()))
elif verb in ("start", "stop"):
    held["active"] = sorted(set(held["active"]) - {{unit}} | ({{unit}} if verb == "start" else set()))
book.write_text(json.dumps(held))
'''


class Host:
    """A host in a directory: its tree, the directories setup writes, fakes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.tree = root / "usr/local/lib/severino-hq"
        self.systemd = root / "etc/systemd/system"
        self.tmpfiles = root / "etc/tmpfiles.d"
        self.sudoers = root / "etc/sudoers.d"
        self.state = root / "state"
        self.bin = root / "bin"
        for directory in (self.systemd, self.tmpfiles, self.sudoers, self.state, self.bin):
            directory.mkdir(parents=True)
        for part in ("systemd", "tmpfiles", "ansible"):
            shutil.copytree(ROOT / "deploy" / part, self.tree / "deploy" / part)
        self.program = self.tree / "deploy/bin/hq-host"
        self.program.parent.mkdir(parents=True)
        for name in ("systemctl", "systemd-analyze", "systemd-tmpfiles", "visudo", "docker"):
            self._fake(self.bin / name, name)
        self._fake(self.program, "hq-host")

    def _fake(self, path: Path, name: str) -> None:
        path.write_text(FAKE.format(python=sys.executable, name=name, state=str(self.state), systemd=str(self.systemd)))
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def shipped(self) -> list[str]:
        """What Shipped (controller/host/units.go) finds in the tree."""
        units = self.tree / "deploy/systemd"
        found = [entry.name for entry in units.iterdir() if entry.is_file()]
        found += [
            f"{entry.name}/{dropin.name}"
            for entry in units.iterdir()
            if entry.is_dir() and entry.name.endswith(".d")
            for dropin in entry.iterdir()
            if dropin.name.endswith(".conf")
        ]
        return sorted(found)

    def variables(self) -> dict:
        """What setupVars (controller/host/setup.go) gives the playbook."""
        suffixes = (".service", ".socket", ".mount", ".automount", ".swap", ".target", ".path", ".timer", ".slice")
        return {
            "tree": str(self.tree),
            "systemd_dir": str(self.systemd),
            "tmpfiles_dir": str(self.tmpfiles),
            "sudoers_dir": str(self.sudoers),
            "shipped": self.shipped(),
            "unit_patterns": [f"severino-hq*{suffix}" for suffix in suffixes],
            "secrets_unit": "severino-hq-secrets.service",
            "web_unit": "severino-hq-web.service",
            "automation_unit": "severino-hq-automation.service",
            "host_program": str(self.program),
            "container": "severino-hq",
            "host": {
                "vault": VAULT,
                "env_item": "example env",
                "connect": {
                    "unit": "example-connect.service",
                    "url": "http://127.0.0.1:880",
                    "credential": "op_connect_example",
                },
                "runner": {"unit": "actions.runner.example.service", "user": "deployer"},
                "automation": True,
            },
        }

    def play(self, *flags: str, refused: bool = False) -> tuple[int, dict, float]:
        """One run, as hq-host makes it, read through ansible.posix.json."""
        variables = self.root / "setup.json"
        variables.write_text(json.dumps(self.variables()))
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{Path(sys.executable).parent}{os.pathsep}/usr/bin:/bin",
            "ANSIBLE_CONFIG": str(self.tree / "deploy/ansible/ansible.cfg"),
            "ANSIBLE_COLLECTIONS_PATH": str(Path(sys.prefix, "share", "ansible", "collections")),
            "ANSIBLE_HOME": str(self.root / "ansible"),
            "ANSIBLE_LOCAL_TEMP": str(self.root / "ansible/local"),
            "ANSIBLE_REMOTE_TMP": str(self.root / "ansible/remote"),
            "ANSIBLE_STDOUT_CALLBACK": "ansible.posix.json",
        }
        command = [
            str(Path(sys.executable).parent / "ansible-playbook"),
            "--inventory",
            "host,",
            "--connection",
            "local",
            "--extra-vars",
            f"setup_vars={variables}",
            "--extra-vars",
            f"ansible_python_interpreter={sys.executable}",
            *flags,
            str(self.tree / "deploy/ansible/host.yml"),
        ]
        started = time.monotonic()
        ran = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=600, check=False)
        took = time.monotonic() - started
        try:
            report = json.loads(ran.stdout)
        except json.JSONDecodeError:
            report = {}
        if not report:
            print(ran.stdout, ran.stderr, sep="\n", file=sys.stderr)
        for play in report.get("plays", []):
            for task in play["tasks"]:
                for result in task["hosts"].values():
                    if result.get("failed") and not refused:
                        print(f"{task['task']['name']}: {result.get('msg', '')}", file=sys.stderr)
        return ran.returncode, report, took

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def held(self) -> dict:
        return json.loads((self.state / "systemd.json").read_text())


def changed(report: dict) -> list[str]:
    """Each task that changed something, by name."""
    return [
        task["task"]["name"]
        for play in report.get("plays", [])
        for task in play["tasks"]
        for result in task["hosts"].values()
        if result.get("changed")
    ]


OWNED_BY_ROOT = {"owner": "root", "group": "root", "pw_name": "root", "gr_name": "root", "uid": 0, "gid": 0}


def as_roots(node: object) -> object:
    """node with every file's owner root's, as on a host, not this account's."""
    if isinstance(node, dict):
        return {key: OWNED_BY_ROOT[key] if key in OWNED_BY_ROOT else as_roots(value) for key, value in node.items()}
    if isinstance(node, list):
        return [as_roots(value) for value in node]
    return node


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--record", type=Path, help="write the drifted check's report here")
    options = parser.parse_args()
    wrong: list[str] = []
    timings: list[tuple[str, float]] = []
    with tempfile.TemporaryDirectory(prefix="hq-host-setup.") as scratch:
        restart = Host(Path(scratch).resolve() / "restart")
        code, _, took = restart.play("--tags", "web")
        timings.append(("apply of what a restart needs (a deploy's first)", took))
        wanted = {name for name in restart.shipped() if "/" in name or name.endswith(".mount")}
        wanted |= {"severino-hq-web.service", "severino-hq-secrets.service"}
        wanted |= {
            "severino-hq-secrets.service.d/10-estate.conf",
            "severino-hq-secrets.service.d/20-connect.conf",
            "actions.runner.example.service.d/10-runner-identity.conf",
        }
        written = {str(path.relative_to(restart.systemd)) for path in restart.systemd.rglob("*") if path.is_file()}
        if code != 0 or written != wanted:
            wrong.append(
                f"what a restart needs is not the drop-ins, the mount, the renderer and HQ alone: {sorted(written ^ wanted)}"
            )
        if any(not call.startswith("systemctl daemon-reload") for call in restart.calls()):
            wrong.append(f"what a restart needs acted on more than its files: {restart.calls()}")

        host = Host(Path(scratch).resolve() / "host")
        code, _, took = host.play()
        timings.append(("apply, a new host", took))
        if code != 0:
            print("A fresh apply failed.", file=sys.stderr)
            return 1
        wrong.extend(
            f"{name} is not installed as the tree holds it"
            for name in host.shipped()
            if (host.systemd / name).read_bytes() != (host.tree / "deploy/systemd" / name).read_bytes()
        )
        estate = (host.systemd / "severino-hq-secrets.service.d/10-estate.conf").read_text()
        if f"SEVERINO_SECRETS_VAULT={VAULT}" not in estate:
            wrong.append(f"host.json's vault was not written as it is: {estate!r}")
        rule = host.sudoers / "severino-hq-deploy"
        if (
            f"deployer ALL=(root) NOPASSWD: {host.program} deploy *" not in rule.read_text()
            or stat.S_IMODE(rule.stat().st_mode) != 0o440
        ):
            wrong.append("the deploy rule is not the runner's one rule, mode 0440")
        if not (host.tmpfiles / "severino-hq.conf").is_file():
            wrong.append("the tmpfiles rule is not installed")
        calls = host.calls()

        def at(prefix: str) -> int:
            return next((i for i, call in enumerate(calls) if call.startswith(prefix)), -1)

        order = [
            at("systemd-tmpfiles --create"),
            at("systemctl start severino-hq-secrets.service"),
            at("hq-host controller"),
            at("docker exec severino-hq hq-controller job content.sync"),
        ]
        if min(order) < 0 or order != sorted(order):
            wrong.append(
                f"the directories, the renderer, the controller preflight and the content preflight ran out of order: {order}"
            )
        held = host.held()
        if (
            "severino-hq-controller.timer" not in held["active"]
            or "severino-hq-controller.timer" not in held["enabled"]
        ):
            wrong.append("a timer was not enabled and started")
        if "severino-hq-controller.service" in held["active"]:
            wrong.append("a unit with no [Install] section was started")

        code, report, took = host.play()
        timings.append(("apply, a converged host (a deploy)", took))
        files = [name for name in changed(report) if name.startswith(("Install", "Render", "Remove", "Make the host"))]
        if code != 0 or files:
            wrong.append(f"a second apply changed files: {files}")
        code, report, took = host.play("--check", "--diff", "--tags", "files")
        timings.append(("check, a converged host (verify)", took))
        if code != 0 or changed(report):
            wrong.append(f"a check after an apply found changes: {changed(report)}")

        web = host.systemd / "severino-hq-web.service"
        web.write_text(web.read_text() + "# edited\n")
        (host.systemd / "severino-hq-old.timer").write_text(
            "[Timer]\nOnBootSec=1h\n\n[Install]\nWantedBy=timers.target\n"
        )
        held = host.held()
        held["enabled"].append("severino-hq-old.timer")
        held["active"].append("severino-hq-old.timer")
        (host.state / "systemd.json").write_text(json.dumps(held))
        estate_path = host.systemd / "severino-hq-secrets.service.d/10-estate.conf"
        estate_path.write_text(estate_path.read_text() + "# edited\n")
        code, report, took = host.play("--check", "--diff", "--tags", "files")
        timings.append(("check, a drifted host", took))
        found = set(changed(report))
        wrong.extend(
            f"a check did not find what {task!r} would change"
            for task in (
                "Install every drop-in and the units the restart needs",
                "Remove the units the release no longer ships",
                "Render the drop-ins host.json decides",
            )
            if task not in found
        )
        if options.record and code == 0:
            # One line, as the check prints it with ANSIBLE_JSON_INDENT=0.
            text = json.dumps(as_roots(report), sort_keys=True).replace(str(host.root), "")
            options.record.write_text(text.replace(str(Path.home()), "~").replace(sys.executable, "python3") + "\n")

        code, _, took = host.play()
        timings.append(("apply, a drifted host", took))
        if code != 0 or (host.systemd / "severino-hq-old.timer").exists():
            wrong.append("an apply did not remove the unit the release no longer ships")
        if not {"systemctl disable severino-hq-old.timer", "systemctl stop severino-hq-old.timer"} <= set(host.calls()):
            wrong.append("the unit the release no longer ships was not stopped and disabled")

        rule.unlink()
        (host.state / "visudo-refuses").touch()
        code, _, _ = host.play(refused=True)
        if code == 0 or rule.exists():
            wrong.append("a deploy rule visudo refused was installed")

    for what, took in timings:
        print(f"{took:6.1f}s  {what}")
    for line in wrong:
        print(f"FAIL {line}", file=sys.stderr)
    return 1 if wrong else 0


if __name__ == "__main__":
    sys.exit(main())
