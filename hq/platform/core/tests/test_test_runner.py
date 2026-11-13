"""What a suite run selects, and what it leaves behind."""

import io
import os
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings, tag
from django.test.runner import DiscoverRunner

from hq.platform.application import agent_limits
from hq.platform.core.run_report import REPORTS, CompositionTextResult
from hq.platform.core.stalls import block_on_output
from hq.platform.core.test_runner import (
    BROWSER_TESTS,
    SOURCE,
    FileClonedParallelSuite,
    SeverinoTestRunner,
    drives_a_browser,
    excluded_tags,
    tests_in,
)


class SourceSelectionTests(SimpleTestCase):
    def test_the_whole_suite_leaves_the_source_tests_out(self):
        self.assertIn(SOURCE, excluded_tags(None, set(), set()))

    def test_asking_for_the_source_tests_runs_them(self):
        self.assertNotIn(SOURCE, excluded_tags(None, {SOURCE}, set()))

    def test_a_named_label_runs_what_it_names(self):
        self.assertNotIn(SOURCE, excluded_tags(["hq.platform.core"], set(), set()))

    def test_other_exclusions_are_kept(self):
        self.assertIn("slow", excluded_tags(None, set(), {"slow"}))


class EmptySelectionTests(SimpleTestCase):
    def test_a_tag_no_test_carries_fails_the_run(self):
        runner = SeverinoTestRunner(tags=["no-test-carries-this"], verbosity=0)
        with self.assertRaisesMessage(RuntimeError, "no-test-carries-this"):
            runner.build_suite([__name__])


class AbandonedRunTests(SimpleTestCase):
    def test_a_killed_runs_folder_is_deleted_though_its_pid_is_another_live_process_now(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # A run's folder another process holds locked, as a run does while it goes.
            (root / "hq-run-a3b4").mkdir()
            holder = subprocess.Popen(
                [sys.executable, "-c", HOLD, str(root / "hq-run-a3b4" / ".owner")],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(holder.wait)
            self.addCleanup(holder.stdin.close)
            self.addCleanup(holder.stdout.close)
            self.assertEqual(holder.stdout.readline().strip(), "held")
            # A run's folder whose process has gone, though its pid is the holder's now: its lock went with it.
            (root / "hq-run-x1y2" / "tmpreceipts").mkdir(parents=True)
            (root / "hq-run-x1y2" / ".owner").write_text(str(holder.pid))
            # A run's folder its process has made and not yet locked.
            (root / "hq-run-c5d6").mkdir()
            (root / "hq-run-c5d6" / ".owner").touch()
            # A copy a shell made, which holds no lock: kept while it may be in use, deleted once older than any run.
            (root / "hq-run-e7f8").mkdir()
            old = root / "hq-run-g9h0"
            old.mkdir()
            two_days_ago = time.time() - 2 * 24 * 60 * 60
            os.utime(old, (two_days_ago, two_days_ago))

            start_a_run(root)

            self.assertEqual(sorted(p.name for p in root.iterdir()), ["hq-run-a3b4", "hq-run-c5d6", "hq-run-e7f8"])


# Holds the lock on the file it is given until its input closes.
HOLD = """
import fcntl, os, sys
handle = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)
fcntl.flock(handle, fcntl.LOCK_EX)
print("held", flush=True)
sys.stdin.read()
"""
# Where the test a nested run runs writes the temporary directory it saw.
SEEN = "HQ_EXAMPLE_TEMPORARY_DIRECTORY_SEEN"
# Opens the database a process names, as any command that reads or writes one does.
OPEN = "from django.db import connection; connection.ensure_connection(); print(connection.settings_dict['NAME'])"
# Loads settings, as every process a gate runs does.
SETTINGS = "import django, os; os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'hq.config.settings'); django.setup()"
# A gate's process that loads settings, then execs itself, as tools/perf/report.py does; again, it
# starts another gate beside it, then says whether the run it owns is still there.
EXECS = f"""
{SETTINGS}
import subprocess, sys
from pathlib import Path
if sys.argv[1:] != ["again"]:
    os.execv(sys.executable, [*sys.orig_argv, "again"])
run = Path(os.environ["SEVERINO_TEST_RUN"])
beside = {{name: value for name, value in os.environ.items() if name not in {{"SEVERINO_TEST_RUN", "SEVERINO_DATABASE_PATH"}}}}
subprocess.run([sys.executable, "-c", {SETTINGS!r}], env=beside | {{"TMPDIR": str(run.parent)}}, check=True)
print(run.is_dir())
"""
# The test task deciding a run is heavy, which it then runs again in the turn by an exec, says what
# the temporary directory it was started in holds as it would exec.
TAKES_THE_TURN = """
import os, sys
from pathlib import Path
from tools.gate import at
def execvp(file, args):
    print(sorted(path.name for path in Path(sys.argv[1]).iterdir()))
    sys.exit(0)
at.os.execvp = execvp
at.take_the_turn(at.Typed(at=None, args=["hq.platform"]))
print("did not take the turn")
"""
# The test database a process inside this run names.
TEST_DATABASE = f"{SETTINGS}; from django.conf import settings; print(settings.DATABASES['default']['TEST']['NAME'])"


def outside_this_run(**extra: str) -> dict[str, str]:
    """The environment of a process started outside this run: none of its folders or its database named to it."""

    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("SEVERINO_TEST_", "SEVERINO_CONTROLLER_"))
        and name
        not in {"SEVERINO_MEDIA_ROOT", "SEVERINO_EXPORTS_ROOT", "SEVERINO_ACTIVITY_MARKER", "SEVERINO_DATABASE_PATH"}
    } | extra


def start_a_run(directory: Path) -> None:
    """Start a gate's process in ``directory``, which deletes what a run that has gone left there, as any run does."""

    subprocess.run(
        [sys.executable, "-c", SETTINGS],
        cwd=settings.BASE_DIR,
        env=outside_this_run(SEVERINO_SANDBOX="1", TMPDIR=str(directory)),
        capture_output=True,
        check=True,
        timeout=300,
    )


class RunFolderTests(SimpleTestCase):
    def test_a_test_makes_its_temporary_files_in_its_runs_own_folder(self):
        seen = Path(tempfile.gettempdir())
        made = Path(tempfile.mkdtemp())
        if report := os.environ.get(SEEN):
            Path(report).write_text(f"{seen}\n{made}\n{settings.DATABASES['default']['NAME']}\n")
        self.assertTrue(seen.name.startswith("hq-run-"), seen)
        self.assertEqual(os.environ["TMPDIR"], str(seen), "what the run starts, a browser's driver included")
        self.assertTrue(made.is_relative_to(seen), made)

    @tag("source")
    def test_a_repository_a_test_makes_has_none_of_gits_template(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["git", "init", "--quiet", directory], check=True)
            made = {path.name for path in Path(directory, ".git").iterdir()}
        self.assertLessEqual({"HEAD", "config", "objects", "refs"}, made)
        self.assertEqual(made & {"hooks", "description", "info"}, set())

    def test_the_runs_folder_and_all_its_tests_made_in_it_are_gone_after_it(self):
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory, "shared")
            shared.mkdir()
            report = Path(directory, "seen")
            label = f"{__name__}.{type(self).__name__}.test_a_test_makes_its_temporary_files_in_its_runs_own_folder"
            done = subprocess.run(
                [sys.executable, "manage.py", "test", label, "--parallel", "1", "--noinput"],
                cwd=settings.BASE_DIR,
                env=outside_this_run(TMPDIR=str(shared), **{SEEN: str(report)}),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            seen, made, database = (Path(line) for line in report.read_text().splitlines())
            self.assertTrue(seen.is_relative_to(shared), seen)
            self.assertTrue(database.is_relative_to(seen), database)
            self.assertFalse(made.exists())
            self.assertFalse(seen.exists())
            # What is meant to outlive a run: the examples Hypothesis replays.
            self.assertEqual({p.name for p in shared.iterdir()} - {"severino-hq-hypothesis"}, set())

    def test_a_gate_naming_no_database_opens_one_in_a_run_folder_of_its_own_gone_after_it(self):
        # Outside a gate, settings refuse to start without one (test_settings_defaults).
        with tempfile.TemporaryDirectory() as directory:
            shared = Path(directory)
            done = subprocess.run(
                [sys.executable, "manage.py", "shell", "-c", OPEN],
                cwd=settings.BASE_DIR,
                env=outside_this_run(SEVERINO_SANDBOX="1", TMPDIR=directory),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            database = Path(done.stdout.strip().splitlines()[-1])
            self.assertTrue(database.parent.name.startswith("hq-run-"), database)
            self.assertTrue(database.is_relative_to(shared), database)
            self.assertEqual(list(shared.iterdir()), [])

    def test_a_gate_that_execs_itself_still_owns_its_run_folder_and_removes_it_as_it_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            done = subprocess.run(
                [sys.executable, "-c", EXECS],
                cwd=settings.BASE_DIR,
                env=outside_this_run(SEVERINO_SANDBOX="1", TMPDIR=directory),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip().splitlines()[-1], "True", "another run's sweep kept it")
            self.assertEqual(list(Path(directory).iterdir()), [])

    @tag("source")
    def test_the_test_task_execs_into_the_turn_owning_no_run_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            done = subprocess.run(
                [sys.executable, "-c", TAKES_THE_TURN, directory],
                cwd=settings.BASE_DIR,
                env=outside_this_run(SEVERINO_SANDBOX="1", TMPDIR=directory, MISE_TASK_LOCKS=""),
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip().splitlines()[-1], "[]")

    def test_a_nested_run_names_a_test_database_apart_from_its_parents(self):
        done = subprocess.run(
            [sys.executable, "-c", TEST_DATABASE],
            cwd=settings.BASE_DIR,
            capture_output=True,
            text=True,
            check=True,
            timeout=300,
        )
        nested = Path(done.stdout.strip().splitlines()[-1])
        own = Path(settings.DATABASES["default"]["TEST"]["NAME"])
        self.assertEqual(nested.parent, own.parent)
        self.assertNotEqual(nested, own)


class BlockingOutputTests(SimpleTestCase):
    def test_a_pipe_left_non_blocking_waits_for_room_again_before_the_report(self):
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        with os.fdopen(write, "w") as stream:
            os.set_blocking(write, False)
            with (
                unittest.mock.patch.object(sys, "stdout", stream),
                unittest.mock.patch.object(sys, "stderr", io.StringIO()),
            ):
                block_on_output()
            self.assertTrue(os.get_blocking(write))


class _Held:
    """Cases a test runs on its own, kept off the module so discovery never
    runs them, and importable by name so a spawned worker can unpickle them."""

    class Prints(SimpleTestCase):
        def test_passes(self):
            print("example.invalid passing print")
            print("example.invalid passing warning", file=sys.stderr)

    class PrintsAndFails(SimpleTestCase):
        def test_fails(self):
            print("example.invalid failing print")
            print("example.invalid failing warning", file=sys.stderr)
            self.fail("example.invalid failure")

    class Stops(SimpleTestCase):
        def test_stops(self):
            breakpoint()


class OutputTests(SimpleTestCase):
    """A run's output is its failures: what a passing test prints is dropped,
    and a failing one's is printed, in a parallel worker as in the parent, and
    the run's progress only when asked."""

    def manage_test(self, *arguments: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        held = f"{__name__}._Held"
        environment = outside_this_run()
        # Its failure is this test's evidence, never one for the gate running this test to report.
        environment.pop(REPORTS, None)
        return subprocess.run(
            [sys.executable, "manage.py", "test", "--noinput", *(a.replace("_Held", held, 1) for a in arguments)],
            cwd=settings.BASE_DIR,
            env=environment,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def run_held(self, *options: str) -> str:
        ran = self.manage_test("_Held.Prints", "_Held.PrintsAndFails", *options)
        self.assertEqual(ran.returncode, 1, ran.stderr[-4000:])
        self.assertIn("example.invalid failure", ran.stderr)
        return ran.stdout + ran.stderr

    def test_a_run_prints_what_a_failing_test_printed_and_nothing_a_passing_one_did(self):
        for parallel in ("1", "2"):
            with self.subTest(parallel=parallel):
                printed = self.run_held("--parallel", parallel)
                self.assertIn("example.invalid failing print", printed)
                self.assertIn("example.invalid failing warning", printed)
                self.assertNotIn("example.invalid passing", printed)
                self.assertNotIn("Found 2 test(s)", printed)

    def test_no_buffer_prints_what_a_passing_test_printed_and_v_1_the_progress(self):
        printed = self.run_held("--parallel", "1", "--no-buffer", "-v", "1")
        self.assertIn("Found 2 test(s)", printed)
        self.assertIn("example.invalid passing print", printed)
        self.assertIn("example.invalid passing warning", printed)

    def test_a_breakpoint_prompts_on_the_runs_own_output(self):
        ran = self.manage_test("_Held.Stops", "--parallel", "1", stdin="c\n")
        self.assertEqual(ran.returncode, 0, ran.stderr[-4000:])
        self.assertIn("(Pdb)", ran.stdout)

    def test_pdb_prompts_on_a_failure(self):
        ran = self.manage_test("_Held.PrintsAndFails", "--pdb", "--parallel", "1", stdin="c\n")
        self.assertEqual(ran.returncode, 1, ran.stderr[-4000:])
        self.assertIn("(Pdb)", ran.stdout)


class BrowserTestTests(SimpleTestCase):
    """A test drives a browser when its class comes from the browser gates'
    package, wherever the class itself is declared (tools/gate/turn.py)."""

    def test_a_test_from_elsewhere_drives_none(self):
        class Plain(SimpleTestCase):
            def test_it(self):
                pass

        self.assertFalse(drives_a_browser(Plain("test_it")))

    def test_a_subclass_of_a_browser_gate_drives_one(self):
        gate = type("Gate", (SimpleTestCase,), {"__module__": f"{BROWSER_TESTS}.gate", "test_it": lambda self: None})

        class Drawn(gate):
            pass

        self.assertTrue(drives_a_browser(Drawn("test_it")))

    def test_the_whole_suite_leaves_out_every_browser_gate_it_discovers_and_still_runs_across_workers(self):
        gate = type("Gate", (SimpleTestCase,), {"__module__": f"{BROWSER_TESTS}.catalog", "test_it": lambda self: None})
        plain = [type(name, (SimpleTestCase,), {"test_it": lambda self: None}) for name in ("One", "Two")]
        found = unittest.TestSuite([gate("test_it"), *(kind("test_it") for kind in plain)])
        with (
            unittest.mock.patch.object(DiscoverRunner, "load_tests_for_label", return_value=found),
            unittest.mock.patch("hq.platform.core.test_runner.plugin_test_modules", return_value=[]),
        ):
            suite = SeverinoTestRunner(verbosity=0, parallel=2).build_suite([])

        self.assertCountEqual([type(test) for test in tests_in(suite)], plain)
        self.assertIsInstance(suite, FileClonedParallelSuite)


class BrowserFailureShotTests(SimpleTestCase):
    def built(self, *tests):
        with unittest.mock.patch.object(DiscoverRunner, "build_suite", return_value=unittest.TestSuite(tests)):
            SeverinoTestRunner(verbosity=0).build_suite(["example"])

    def test_a_browser_run_replaces_its_tasks_shots_in_the_checkout_and_no_other_run_does(self):
        gate = type("Gate", (SimpleTestCase,), {"__module__": f"{BROWSER_TESTS}.gate", "test_it": lambda self: None})
        plain = type("Plain", (SimpleTestCase,), {"test_it": lambda self: None})
        with (
            tempfile.TemporaryDirectory() as checkout,
            override_settings(BASE_DIR=checkout),
            unittest.mock.patch.dict(os.environ, {"MISE_TASK_NAME": "browser:example"}),
        ):
            shots = Path(checkout, "var", "browser-failures")
            (shots / "browser-example" / "test_it-last").mkdir(parents=True)
            (shots / "browser-other" / "test_it-kept").mkdir(parents=True)

            self.built(plain("test_it"))
            self.assertEqual(sorted(p.name for p in shots.iterdir()), ["browser-example", "browser-other"])

            self.built(gate("test_it"))
            self.assertEqual(sorted(p.name for p in shots.iterdir()), ["browser-other"])


@override_settings(SEVERINO_AGENT_CALLS_PER_MINUTE=1, SEVERINO_AGENT_CALLS_AT_ONCE_OVERALL=1)
class AgentLimitsIsolationTests(SimpleTestCase):
    """A test reads the agent limits as if no test ran before it in the process."""

    def test_a_reader_after_a_test_that_left_a_call_and_a_spent_rate_finds_neither(self):
        left = []

        class Leaving(unittest.TestCase):
            def test_it(self):
                agent_limits.LIMITS.take("example-left")
                left.append((agent_limits.LIMITS, agent_limits.LIMITS.occupy("example-left")))

        class Reading(unittest.TestCase):
            def test_it(self):
                self.assertEqual(agent_limits.LIMITS.held(), 0)
                self.assertIsInstance(agent_limits.LIMITS.occupy("example-next"), agent_limits.Slot)

        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        unittest.TestSuite([Leaving("test_it"), Reading("test_it")]).run(result)

        self.assertTrue(result.wasSuccessful(), result.failures)
        [(taken_from, slot)] = left
        slot.__exit__(None, None, None)
        self.assertIsInstance(
            taken_from.occupy("example-other"), agent_limits.Slot, "the call left running gives back to its own set"
        )

    def test_what_a_class_sets_up_reaches_each_of_its_tests_and_no_test_reaches_the_next(self):
        seen = []

        class SettingUp(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                agent_limits.LIMITS.listen("example-class")

            def test_first(self):
                seen.append(agent_limits.LIMITS.listening("example-class"))
                agent_limits.LIMITS.listen("example-class")
                agent_limits.LIMITS.listen("example-test")

            def test_second(self):
                seen.append(agent_limits.LIMITS.listening("example-class"))
                seen.append(agent_limits.LIMITS.listening("example-test"))

        class After(unittest.TestCase):
            def test_it(self):
                seen.append(agent_limits.LIMITS.held())

        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        loader = unittest.TestLoader()
        unittest.TestSuite([loader.loadTestsFromTestCase(SettingUp), loader.loadTestsFromTestCase(After)]).run(result)

        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        first, second_class, second_test, after = seen
        self.assertEqual((first, second_class), (1, 1), "the class's stream reaches each of its tests")
        self.assertEqual(second_test, 0, "a stream one test opened is gone from the next")
        self.assertEqual(after, 0, "the next class sets up on a new set")


class MarkerIsolationTests(SimpleTestCase):
    """A test finds the controller's markers as its class's setup left them,
    whatever a test before it wrote, in this process or another."""

    def test_what_a_class_writes_reaches_each_of_its_tests_and_no_test_reaches_the_next(self):
        from hq.platform.application import cadence

        heartbeat, doorbell = Path(settings.SEVERINO_CONTROLLER_HEARTBEAT), Path(settings.SEVERINO_CONTROLLER_DOORBELL)
        seen = []

        class SettingUp(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                cadence.note_controller()

            def test_first(self):
                seen.append(heartbeat.exists())
                heartbeat.unlink()
                cadence.ring_doorbell()

            def test_second(self):
                seen.append(heartbeat.exists())
                seen.append(doorbell.exists())

        class After(unittest.TestCase):
            def test_it(self):
                seen.append(heartbeat.exists() or doorbell.exists())

        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        loader = unittest.TestLoader()
        unittest.TestSuite([loader.loadTestsFromTestCase(SettingUp), loader.loadTestsFromTestCase(After)]).run(result)

        self.assertTrue(result.wasSuccessful(), result.failures + result.errors)
        first, second, rung, after = seen
        self.assertEqual((first, second), (True, True), "the class's heartbeat reaches each of its tests")
        self.assertFalse(rung, "a doorbell one test rang is gone from the next")
        self.assertFalse(after, "the next class sets up with no markers")

    def test_each_process_keeps_its_markers_apart(self):
        from hq.platform.core import isolation_testing as isolation

        here = isolation.own_markers()
        kept = {setting: getattr(settings, setting) for setting, _ in isolation.MARKERS}
        try:
            with unittest.mock.patch("hq.platform.core.isolation_testing.os.getpid", lambda: -1):
                other = isolation.own_markers()
                heartbeat = settings.SEVERINO_CONTROLLER_HEARTBEAT
        finally:
            isolation._markers = (os.getpid(), here)
            for setting, value in kept.items():
                setattr(settings, setting, value)

        self.assertNotEqual(other, here)
        self.assertEqual(Path(heartbeat).parent, other)
