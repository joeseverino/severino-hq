"""The deploy host's readings find HQ's container without docker, and a reading that fails stays in its place."""

import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tools.pipeline import release_readings

SCOPE = "a" * 64
OTHER = "b" * 64
# The cgroup files as a cgroup v2 kernel writes them.
CPU_STAT = "usage_usec 25635004\nuser_usec 21455770\nsystem_usec 4179234\nnr_periods 0\n"
MEMORY_STAT = "anon 147906560\nfile 20000000\nanon_thp 50331648\n"
HQ = b"/usr/local/bin/python\0/usr/local/bin/uvicorn\0hq.config.asgi:application\0--port\0008000\0"


def process(proc: Path, pid: int, argv: bytes, cgroup: str) -> None:
    folder = proc / str(pid)
    folder.mkdir()
    (folder / "cmdline").write_bytes(argv)
    (folder / "cgroup").write_text(f"0::{cgroup}\n")
    # Field 22, the start in clock ticks, after a name holding a space.
    (folder / "stat").write_text(f"{pid} (uv icorn) S 1 {pid} {pid} 0 -1 4194560 " + "0 " * 12 + "1000 0\n")


def scope(cgroups: Path, ident: str, limit: str = "1073741824") -> None:
    folder = cgroups / "system.slice" / f"docker-{ident}.scope"
    folder.mkdir(parents=True)
    for name, text in {
        "memory.current": "176570368",
        "memory.peak": "216969216",
        "memory.max": limit,
        "memory.stat": MEMORY_STAT,
        "cpu.stat": CPU_STAT,
    }.items():
        (folder / name).write_text(text + "\n")


class ContainerTests(SimpleTestCase):
    def setUp(self):
        held = tempfile.TemporaryDirectory()
        self.addCleanup(held.cleanup)
        root = Path(held.name)
        self.proc, self.cgroups = root / "proc", root / "cgroup"
        self.proc.mkdir()
        (self.proc / "uptime").write_text("1000.00 900.00\n")
        (self.proc / "self").mkdir()
        process(self.proc, 7, b"/sbin/init\0", "/init.scope")

    def test_the_serving_process_names_its_container_and_its_numbers_are_read(self):
        process(self.proc, 42, HQ, f"/system.slice/docker-{SCOPE}.scope")
        process(self.proc, 43, b"/usr/bin/postgres\0", f"/system.slice/docker-{OTHER}.scope")
        scope(self.cgroups, SCOPE)
        scope(self.cgroups, OTHER)
        found = release_readings.container(self.proc, self.cgroups)
        self.assertEqual(found["container"], SCOPE[:12])
        self.assertEqual(found["memory_peak"], 216969216)
        self.assertEqual(found["anon"], 147906560)
        self.assertEqual(found["cpu_usec"], 25635004)
        self.assertAlmostEqual(found["age_s"], 1000 - 1000 / os.sysconf("SC_CLK_TCK"), places=1)
        self.assertIn("/proc/42/cgroup", found["method"])

    def test_no_limit_is_none(self):
        process(self.proc, 42, HQ, f"/system.slice/docker-{SCOPE}.scope")
        scope(self.cgroups, SCOPE, limit="max")
        self.assertIsNone(release_readings.container(self.proc, self.cgroups)["memory_max"])

    def test_two_containers_serving_is_refused_rather_than_guessed(self):
        process(self.proc, 42, HQ, f"/system.slice/docker-{SCOPE}.scope")
        process(self.proc, 52, HQ, f"/system.slice/docker-{OTHER}.scope")
        with self.assertRaisesRegex(RuntimeError, "2 docker scopes"):
            release_readings.container(self.proc, self.cgroups)

    def test_a_serving_process_outside_docker_is_not_production(self):
        process(self.proc, 42, HQ, "/user.slice/user-1000.slice/session-1.scope")
        with self.assertRaisesRegex(RuntimeError, "no process"):
            release_readings.container(self.proc, self.cgroups)

    def test_a_failed_reading_is_its_error_and_the_rest_are_still_read(self):
        with mock.patch.object(release_readings, "ran", return_value="severino-hq-x.service loaded failed failed X\n"):
            found = release_readings.readings(self.proc, self.cgroups)
        self.assertIn("no process", found["container"]["error"])
        self.assertEqual(found["failed"], {"value": ["severino-hq-x.service"]})


class DriftTests(SimpleTestCase):
    def test_the_verdict_is_what_its_last_run_printed(self):
        shown = "InvocationID=71a61a8586104300a5946a2159366c48\nResult=exit-code\nExecMainStatus=1\n"
        with mock.patch.object(release_readings, "ran", side_effect=[shown, "drift: could not list\n"]) as ran:
            found = release_readings.drift()
        self.assertEqual(found["said"], ["drift: could not list"])
        self.assertIn("_SYSTEMD_INVOCATION_ID=71a61a8586104300a5946a2159366c48", ran.call_args.args)

    def test_a_unit_that_never_ran_reads_no_journal(self):
        with mock.patch.object(release_readings, "ran", return_value="InvocationID=\nResult=success\n") as ran:
            self.assertEqual(release_readings.drift()["said"], [])
        self.assertEqual(ran.call_count, 1)
