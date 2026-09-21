"""Exercise installer ordering and rollback without host privileges."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent


class ControllerInstallTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bin = self.root / "bin"
        self.lib = self.root / "lib"
        self.units = self.root / "systemd"
        self.runtime = self.root / "run"
        for path in (self.bin, self.lib / "scripts/lib", self.units, self.runtime):
            path.mkdir(parents=True)
        shutil.copytree(ROOT / "deploy/systemd", self.lib / "deploy/systemd")
        self.unit = self.units / "severino-hq-secrets.service"
        self.unit.write_text("previous unit\n")
        self.legacy = self.runtime / "severino-hq/severino_controller_env"
        self.legacy.parent.mkdir()
        self.legacy.write_text("previous credential\n")
        self.log = self.root / "calls"
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "TEST_ROOT": str(self.root), "TEST_FAIL": ""}
        # Relocate fixed host paths in the test copy, not in the production API.
        source = (ROOT / "scripts/install-controller.sh").read_text()
        for original, target in (
            ("/usr/local/lib/severino-hq", self.lib),
            ("/usr/local/sbin", self.bin),
            ("/etc/systemd/system", self.units),
            ("/var/log/severino-hq", self.root / "logs"),
            ("/run/", str(self.runtime) + "/"),
        ):
            source = source.replace(original, str(target))
        self.installer = self.lib / "scripts/install-controller.sh"
        self.installer.write_text(source)
        (self.lib / "scripts/lib/controller-env.sh").write_text(
            'controller_require_environment() { test -f "$TEST_ROOT/rendered"; }\n'
        )
        self.stub(self.bin / "id", "echo 0")
        self.stub(self.bin / "chown", "exit 0")
        self.stub(self.bin / "severino-hq-sync-scripts", 'echo sync >>"$TEST_ROOT/calls"')
        self.stub(self.bin / "systemd-analyze", 'echo verify >>"$TEST_ROOT/calls"')
        self.stub(self.bin / "install", '''
while [ "$#" -gt 0 ]; do
    case "$1" in
        -d) shift; while [ "$#" -gt 1 ]; do shift; done; mkdir -p "$1"; exit ;;
        -o|-g|-m) shift 2 ;;
        *) break ;;
    esac
done
cp "$1" "$2"
''')
        self.stub(self.bin / "systemctl", '''
echo "$*" >>"$TEST_ROOT/calls"
case "$1" in
    enable) [ "$TEST_FAIL" != activation ] ;;
    start)
        grep -q 'severino-hq-secrets' "$TEST_ROOT/systemd/severino-hq-secrets.service"
        [ "$TEST_FAIL" != render ] || exit 1
        touch "$TEST_ROOT/rendered" ;;
esac
''')
        for name in ("install-cosign", "provision-controller-ssh"):
            self.stub(self.lib / f"scripts/{name}.sh", f'echo {name} >>"$TEST_ROOT/calls"')
        self.stub(self.lib / "scripts/run-private.sh", '''
echo preflight >>"$TEST_ROOT/calls"
[ "$TEST_FAIL" != preflight ]
''')

    def stub(self, path, body):
        path.write_text("#!/bin/sh\nset -eu\n" + body + "\n")
        path.chmod(0o700)

    def run_installer(self):
        return subprocess.run(["sh", str(self.installer)], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def test_unit_is_installed_and_reloaded_before_render_and_preflight(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.log.read_text().splitlines()
        render = calls.index("start severino-hq-secrets.service")
        self.assertLess(calls.index("verify"), calls.index("daemon-reload"))
        self.assertLess(calls.index("daemon-reload"), render)
        self.assertLess(render, calls.index("provision-controller-ssh"))
        self.assertLess(render, calls.index("preflight"))
        self.assertEqual(self.unit.read_bytes(),
                         (self.lib / "deploy/systemd/severino-hq-secrets.service").read_bytes())
        self.assertEqual(list(self.runtime.glob("severino-hq-unit.*")), [])
        self.assertFalse(self.legacy.exists())

    def test_failed_render_or_preflight_restores_previous_unit(self):
        for failure in ("render", "preflight", "activation"):
            with self.subTest(failure=failure):
                self.env["TEST_FAIL"] = failure
                self.log.write_text("")
                result = self.run_installer()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.unit.read_text(), "previous unit\n")
                calls = self.log.read_text().splitlines()
                self.assertEqual(calls[-1], "daemon-reload")
                if failure != "activation":
                    self.assertFalse(any(call.startswith("enable ") for call in calls))
                self.assertEqual(self.legacy.read_text(), "previous credential\n")
                self.assertEqual(list(self.runtime.glob("severino-hq-unit.*")), [])

    def test_failed_first_install_removes_new_unit(self):
        self.unit.unlink()
        self.env["TEST_FAIL"] = "render"
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.unit.exists())
