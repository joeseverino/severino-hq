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
        # The render's drop-in travels with its unit, so it is rolled back with it.
        self.dropin = self.units / "severino-hq-secrets.service.d/10-root-owned-exec.conf"
        self.dropin.parent.mkdir()
        self.dropin.write_text("previous drop-in\n")
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
        # The real unit library: what it derives is what these tests are about.
        shutil.copy(ROOT / "scripts/lib/systemd-units.sh", self.lib / "scripts/lib")
        (self.lib / "scripts/lib/controller-env.sh").write_text(
            'controller_require_environment() { test -f "$TEST_ROOT/rendered"; }\n'
        )
        self.stub(self.bin / "id", "echo 0")
        self.stub(self.bin / "chown", "exit 0")
        self.stub(self.bin / "severino-hq-sync-scripts", 'echo sync >>"$TEST_ROOT/calls"')
        self.stub(self.bin / "systemd-analyze", '''
echo verify >>"$TEST_ROOT/calls"
shift
printf '%s\\n' "$@" >"$TEST_ROOT/verified"
''')
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
        for name in ("install-cosign",):
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
                self.assertEqual(self.dropin.read_text(), "previous drop-in\n")
                calls = self.log.read_text().splitlines()
                self.assertEqual(calls[-1], "daemon-reload")
                if failure != "activation":
                    self.assertFalse(any(call.startswith("enable ") for call in calls))
                self.assertEqual(self.legacy.read_text(), "previous credential\n")
                self.assertEqual(list(self.runtime.glob("severino-hq-unit.*")), [])

    def test_failed_first_install_removes_new_unit(self):
        self.unit.unlink()
        self.dropin.unlink()
        self.env["TEST_FAIL"] = "render"
        self.assertNotEqual(self.run_installer().returncode, 0)
        self.assertFalse(self.unit.exists())
        self.assertFalse(self.dropin.exists())

    def shipped(self):
        """What systemd would read from the shipped tree, walked independently."""
        tree = self.lib / "deploy/systemd"
        return sorted(
            str(path.relative_to(tree)) for path in tree.rglob("*")
            if path.is_file() and not path.name.endswith(".example")
        )

    def test_a_unit_added_to_the_repository_is_installed_and_enabled(self):
        # Nothing in the installer names these. A unit that has to be added to a
        # list as well as to deploy/systemd is one that reaches the host only
        # when somebody remembers the list.
        tree = self.lib / "deploy/systemd"
        (tree / "severino-hq-example.service").write_text("[Service]\nExecStart=/bin/true\n")
        (tree / "severino-hq-example.timer").write_text("[Timer]\nOnCalendar=daily\n")
        (tree / "severino-hq-example.service.d").mkdir()
        (tree / "severino-hq-example.service.d/10-example.conf").write_text("[Service]\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in self.shipped():
            with self.subTest(name=name):
                self.assertEqual((self.units / name).read_bytes(), (tree / name).read_bytes())
        top_level = [name for name in self.shipped() if "/" not in name]
        self.assertEqual((self.root / "verified").read_text().split(),
                         [str(tree / name) for name in top_level])
        enabled = [call.split()[2:] for call in self.log.read_text().splitlines()
                   if call.startswith("enable --now ")]
        self.assertEqual(enabled, [[name for name in top_level
                                    if name.endswith((".timer", ".path"))]])
        self.assertIn("severino-hq-example.timer", enabled[0])

    def test_templates_and_host_owned_drop_ins_are_left_alone(self):
        tree = self.lib / "deploy/systemd"
        host_owned = self.dropin.parent / "10-estate.conf"
        host_owned.write_text("host naming\n")
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(host_owned.read_text(), "host naming\n")
        templates = [path.name for path in tree.glob("*.example")]
        self.assertTrue(templates)
        self.assertEqual([name for name in templates if (self.units / name).exists()], [])
