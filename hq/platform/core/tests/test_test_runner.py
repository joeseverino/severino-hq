"""What a suite run selects, and what it leaves behind."""

import contextlib
import io
import json
import multiprocessing
import os
import pickle
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
from dataclasses import asdict
from pathlib import Path
from typing import override

from django.conf import settings
from django.test import Client, SimpleTestCase, override_settings, tag
from django.test.runner import DiscoverRunner

from hq.platform.application import agent_limits
from hq.platform.core.test_runner import (
    BROWSER_TESTS,
    DEFAULT_STALL_SECONDS,
    REPORTS,
    SOURCE,
    STALL_SECONDS,
    ClassTimedSuite,
    CompositionRemoteResult,
    CompositionTextResult,
    FileClonedParallelSuite,
    SeverinoTestRunner,
    Stalled,
    StallWatch,
    block_on_output,
    drives_a_browser,
    excluded_tags,
    report_class_times,
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


def _timed_classes(clock):
    """Two classes whose fixtures and test advance ``clock`` by known amounts."""

    class Slow(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            clock[0] += 5

        def test_it(self):
            clock[0] += 1

    class Quick(unittest.TestCase):
        def test_it(self):
            clock[0] += 2

    return Slow, Quick


class ClassTimeTests(SimpleTestCase):
    """Each class's wall time reaches the run's result, its fixtures included,
    whether it ran here or in a parallel worker."""

    def run_timed(self, result):
        """The tests run, as the parent holds them: a suite empties itself as it runs."""

        clock = [0.0]
        slow, quick = _timed_classes(clock)
        loader = unittest.defaultTestLoader
        tests = [*loader.loadTestsFromTestCase(slow), *loader.loadTestsFromTestCase(quick)]
        with unittest.mock.patch("hq.platform.core.test_runner.monotonic", lambda: clock[0]):
            ClassTimedSuite(tests).run(result)
        return tests

    def times(self, result):
        return {name.rsplit(".", 1)[-1]: seconds for name, seconds in result.class_times.items()}

    def test_a_serial_run_times_each_class_with_its_set_up(self):
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        self.run_timed(result)

        self.assertEqual(self.times(result), {"Slow": 6, "Quick": 2})

    def test_a_workers_times_reach_the_parent(self):
        worker = CompositionRemoteResult()
        tests = self.run_timed(worker)

        parent = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        self.assertEqual(self.times(parent), {"Slow": 6, "Quick": 2})

    def test_the_report_puts_the_slowest_first_beside_the_load(self):
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        result.class_times = {"example.Quick": 2.0, "example.Slow": 6.0}
        stream = io.StringIO()
        report_class_times(result, stream, (1.0, 2.0, 3.0))

        said = stream.getvalue()
        self.assertIn("load average", said)
        self.assertLess(said.index("example.Slow"), said.index("example.Quick"))


def _failing_class():
    """A failure, an error, a test whose subtests fail, and a pass."""

    class Failing(unittest.TestCase):
        def test_compares(self):
            self.assertEqual("expected", "found")

        def test_raises(self):
            raise LookupError("no example.invalid record")

        def test_subtests(self):
            for n in range(2):
                with self.subTest(n=n):
                    self.assertEqual(n, -1)

        def test_passes(self):
            pass

    return Failing


class FailureRecordTests(SimpleTestCase):
    """Each failed test reaches the gate running the suite as the result saw
    it, whether it ran here or in a parallel worker."""

    def run_failing(self, result):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_failing_class()))
        unittest.TestSuite(tests).run(result)
        return tests

    def assert_recorded(self, result):
        failed = {each.test.rsplit(".", 1)[-1]: each for each in vars(result)["failed"].values()}
        self.assertEqual(set(failed), {"test_compares", "test_raises", "test_subtests"})
        self.assertEqual(failed["test_compares"].exception, "AssertionError: 'expected' != 'found'")
        self.assertEqual(failed["test_raises"].exception, "LookupError: no example.invalid record")
        self.assertIn('raise LookupError("no example.invalid record")', failed["test_raises"].traceback)
        self.assertEqual(failed["test_subtests"].exception, "AssertionError: 0 != -1")

    def test_a_serial_run_records_each_failed_test_once(self):
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        self.run_failing(result)

        self.assert_recorded(result)

    def test_a_workers_failures_reach_the_parent_with_their_tracebacks(self):
        # A class local to a function does not pickle, so this holds that a
        # worker sends none of a failed subtest's case.
        worker = CompositionRemoteResult()
        tests = self.run_failing(worker)

        parent = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        self.assert_recorded(parent)

    def run_suite(self, folder: str | None):
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(_failing_class())
        with unittest.mock.patch.dict(os.environ), contextlib.redirect_stderr(io.StringIO()):
            os.environ.pop(REPORTS, None)
            if folder:
                os.environ[REPORTS] = folder
            return SeverinoTestRunner(verbosity=0).run_suite(suite)

    def test_the_run_writes_them_where_its_gate_reads_them(self):
        with tempfile.TemporaryDirectory() as folder:
            result = self.run_suite(folder)

            written = [json.loads(path.read_text()) for path in Path(folder).iterdir()]
        self.assertEqual(written, [[asdict(each) for each in vars(result)["failed"].values()]])
        self.assert_recorded(result)

    def test_a_run_no_gate_runs_writes_nothing(self):
        with unittest.mock.patch.object(Path, "write_text") as write:
            self.run_suite(None)

        write.assert_not_called()


class UnpicklableError(Exception):
    """An exception holding what cannot pickle, as a jsonschema error holds its validator's functions."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.check = lambda: message


class UnpicklableFailure(AssertionError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.check = lambda: message


def _unpicklable_class():
    class Unpicklable(unittest.TestCase):
        def test_errors(self):
            raise UnpicklableError("example.invalid schema refused")

        def test_fails(self):
            raise UnpicklableFailure("example.invalid page differs")

        def test_subtest_fails(self):
            with self.subTest(n=1):
                raise UnpicklableFailure("example.invalid subtest differs")

    return Unpicklable


class UnpicklableFailureTests(SimpleTestCase):
    """A worker's failure whose exception does not pickle still reaches the
    parent as the failure it is, its exception's text and the line it was
    raised on, rather than ending the run on multiprocessing's error."""

    def test_each_reaches_the_parent_as_its_text_and_kind(self):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_unpicklable_class()))
        worker = CompositionRemoteResult()
        unittest.TestSuite(tests).run(worker)

        parent = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        for event in pickle.loads(pickle.dumps(worker.events)):
            FileClonedParallelSuite.handle_event(None, parent, tests, event)
        failed = {each.test.rsplit(".", 1)[-1]: each for each in vars(parent)["failed"].values()}
        self.assertEqual(
            {name: each.exception for name, each in failed.items()},
            {
                "test_errors": f"{__name__}.UnpicklableError: example.invalid schema refused",
                "test_fails": f"{__name__}.UnpicklableFailure: example.invalid page differs",
                "test_subtest_fails": f"{__name__}.UnpicklableFailure: example.invalid subtest differs",
            },
        )
        self.assertIn('raise UnpicklableError("example.invalid schema refused")', failed["test_errors"].traceback)
        self.assertEqual((len(parent.errors), len(parent.failures)), (1, 2))


class _Held:
    """Cases a test runs on its own, kept off the module so discovery never
    runs them, and importable by name so a spawned worker can unpickle them."""

    class ClientPages(unittest.TestCase):
        """A case holding a test client ready to serve, whose handler does not
        pickle. Not a SimpleTestCase, which leaves such an attribute out when
        it is pickled."""

        def setUp(self):
            self.client = Client()
            self.client.handler.load_middleware()

        def test_each(self):
            for page in ("first", "second"):
                with self.subTest(page=page):
                    self.assertEqual(page, "first", "example.invalid page")

        def test_passes(self):
            pass

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


def report_both_ways(path: str) -> None:
    """Run ``_Held.ClientPages`` serially and in two workers, and write what each result lists to ``path``."""

    import django

    django.setup()
    report = {}
    for way in ("serial", "parallel"):
        tests = list(unittest.defaultTestLoader.loadTestsFromTestCase(_Held.ClientPages))
        result = CompositionTextResult(io.StringIO(), descriptions=True, verbosity=0)
        if way == "serial":
            ClassTimedSuite(tests).run(result)
        else:
            suite = FileClonedParallelSuite([ClassTimedSuite([test]) for test in tests], processes=2)
            suite.serialized_aliases = suite.used_aliases = set()
            suite.run(result)
        report[way] = {
            "failures": [[str(test), test.id(), printed] for test, printed in result.failures],
            "errors": [[str(test), printed] for test, printed in result.errors],
            "successful": result.wasSuccessful(),
            "failed": sorted(vars(result).get("failed", {})),
        }
    Path(path).write_text(json.dumps(report))


class ParallelSubtestTests(SimpleTestCase):
    """A failed subtest in a parallel worker reaches the run's result under its
    own name, as a serial run reports it, though its case does not pickle."""

    def test_a_subtest_whose_case_holds_a_client_is_reported_as_a_serial_run_reports_it(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder, "report.json")
            # Its own process: a parallel worker, as this test may run in, may
            # not start workers of its own.
            ran = subprocess.run(
                [sys.executable, "-c", f"from {__name__} import report_both_ways; report_both_ways({str(path)!r})"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
            self.assertEqual(ran.returncode, 0, ran.stderr[-4000:])
            report = json.loads(path.read_text())

        serial, parallel = report["serial"], report["parallel"]
        self.assertEqual(
            [each[:2] for each in parallel["failures"]], [each[:2] for each in serial["failures"]], parallel
        )
        [(name, test_id, printed)] = parallel["failures"]
        self.assertIn("page='second'", name)
        self.assertIn("page='second'", test_id)
        self.assertIn("example.invalid page", printed)
        self.assertEqual(parallel["errors"], [])
        self.assertFalse(parallel["successful"])
        self.assertEqual(parallel["failed"], serial["failed"])


class _Marks(StallWatch):
    def __init__(self):
        super().__init__(seconds=600)
        self.marks = []

    @override
    def mark(self, label):
        self.marks.append(label)
        super().mark(label)


class StallWatchTests(SimpleTestCase):
    """A test process that stops making progress says what it was running and
    where every thread is, and only a process running tests does."""

    def written(self, watch):
        watch.stream.seek(0)
        return watch.stream.read()

    def watch(self, seconds, kind=StallWatch):
        watch = kind(seconds=seconds)
        # A real file: faulthandler writes the stacks to its descriptor.
        watch.stream = self.enterContext(tempfile.TemporaryFile("w+"))
        return watch

    def test_the_report_names_what_stalled_shows_the_evidence_and_every_stack(self):
        watch = self.watch(60)
        watch.evidence.append(lambda stream: stream.write("browser launched\n"))
        watch.report("example.Tests.test_it")

        said = self.written(watch)
        self.assertIn("example.Tests.test_it", said)
        self.assertIn("browser launched", said)
        self.assertIn("test_the_report_names_what_stalled_shows_the_evidence_and_every_stack", said)

    def test_the_report_shows_a_call_waiting_on_another_greenlet(self):
        # As Playwright's sync API leaves a test: switched away mid-call. The
        # greenlet package comes only with the browser group, so a stand-in
        # carries its documented surface: the type, and gr_frame on a
        # suspended one.
        class Suspended:
            def __init__(self, frame):
                self.gr_frame = frame

        def waiting_on_the_browser():
            yield

        call = waiting_on_the_browser()
        next(call)
        self.addCleanup(call.close)
        self.waiting = Suspended(call.gi_frame)
        stand_in = unittest.mock.Mock(greenlet=Suspended)
        watch = self.watch(60)
        with unittest.mock.patch.dict(sys.modules, {"greenlet": stand_in}):
            watch.report("example.Tests.test_it")

        self.assertIn("waiting_on_the_browser", self.written(watch))

    def test_the_report_reaches_the_process_stderr_though_a_test_holds_sys_stderr(self):
        # --buffer swaps sys.stderr for a buffer it prints only once the test
        # fails, which a process that exits on the stall never reaches.
        watch = StallWatch(seconds=60)
        stderr = self.enterContext(tempfile.TemporaryFile("w+"))
        held = io.StringIO()
        with unittest.mock.patch.object(sys, "__stderr__", stderr), contextlib.redirect_stderr(held):
            watch.report("example.Tests.test_hangs")
            with unittest.mock.patch.object(os, "_exit"):
                watch.end("example.Tests.test_hangs")
        stderr.seek(0)
        written = stderr.read()
        self.assertIn("stalled 60s: example.Tests.test_hangs", written)
        self.assertIn("example.Tests.test_hangs did not stop", written)
        self.assertEqual(held.getvalue(), "")

    def test_a_test_that_patches_the_clock_is_not_stalled_by_it(self):
        # As a test that fakes time.monotonic from its setUp leaves the watch:
        # its deadline set on the real clock, then woken under the fake one.
        watch = self.watch(60)
        with watch.watching():
            watch.mark("example.Tests.test_clocked")
            with unittest.mock.patch("time.monotonic", lambda: 10.0**12):
                with watch.watching():
                    pass
                threading.Event().wait(0.2)
        self.assertNotIn("example.Tests.test_clocked", self.written(watch))

    def test_a_mark_left_unfollowed_fails_what_it_marked_after_its_report(self):
        watch = self.watch(0.01)
        with self.assertRaises(Stalled), watch.watching():
            watch.mark("example.Tests.test_stuck")
            threading.Event().wait(60)
        self.assertIn("example.Tests.test_stuck", self.written(watch))

    def test_a_test_that_will_not_stop_ends_its_process(self):
        ended = threading.Event()

        class Ending(StallWatch):
            @override
            def end(self, label):
                ended.set()

        watch = self.watch(0.01, Ending)
        watch.grace = 0.05
        with watch.watching():
            watch.mark("example.Tests.test_swallows")
            while not ended.is_set():
                with contextlib.suppress(Stalled):
                    self.assertTrue(ended.wait(60))

    def test_a_worker_that_ends_stops_its_parent_and_exits_failed(self):
        watch = self.watch(60)
        watch.workers = multiprocessing.Value("i", 2)
        with unittest.mock.patch.object(os, "_exit") as exited:
            watch.end("example.Tests.test_swallows")
        self.assertLess(watch.workers.value, 0)
        self.assertNotEqual(exited.call_args.args[0], 0)

    def test_a_pause_holds_no_deadline_and_restarts_the_bound(self):
        watch = self.watch(600)
        with watch.watching():
            watch.mark("example.Tests.test_waits")
            with watch.paused():
                self.assertIsNone(watch._deadline)
            self.assertIsNotNone(watch._deadline)
            self.assertEqual(watch._label, "example.Tests.test_waits")

    def test_a_process_that_only_replays_results_is_not_watched(self):
        watch = self.watch(0.01)
        watch.mark("example.Tests.test_it")
        self.assertIsNone(watch._thread)

    def test_unset_the_default_bound_watches(self):
        with unittest.mock.patch.dict(os.environ, {STALL_SECONDS: ""}):
            self.assertEqual(StallWatch.from_environment().seconds, DEFAULT_STALL_SECONDS)

    def test_zero_watches_nothing(self):
        with unittest.mock.patch.dict(os.environ, {STALL_SECONDS: "0"}):
            watch = StallWatch.from_environment()
        with watch.watching():
            watch.mark("example.Tests.test_it")
        self.assertFalse(watch.enabled)
        self.assertIsNone(watch._thread)

    def test_a_run_inside_a_test_gives_the_outer_test_back_its_watch(self):
        watch = self.watch(600)
        with watch.watching():
            watch.mark("outer")
            with watch.watching():
                watch.mark("inner")
            self.assertEqual(watch._label, "outer")
        self.assertIsNone(watch._deadline)

    def test_a_suite_marks_each_class_fixture_and_test(self):
        watch = _Marks()
        slow, quick = _timed_classes([0.0])
        loader = unittest.defaultTestLoader
        tests = [*loader.loadTestsFromTestCase(slow), *loader.loadTestsFromTestCase(quick)]
        with unittest.mock.patch("hq.platform.core.test_runner.stall_watch", watch):
            ClassTimedSuite(tests).run(CompositionRemoteResult())

        self.assertEqual(
            [mark.rsplit(".", 1)[-1] for mark in watch.marks],
            ["Slow setUpClass", "test_it", "Slow tearDownClass", "Quick setUpClass", "test_it", "Quick tearDownClass"],
        )

    def test_a_subtest_that_ends_is_progress(self):
        class Pages(unittest.TestCase):
            def test_each(self):
                for page in ("first", "second"):
                    with self.subTest(page=page):
                        pass

        watch = _Marks()
        with unittest.mock.patch("hq.platform.core.test_runner.stall_watch", watch):
            ClassTimedSuite(unittest.defaultTestLoader.loadTestsFromTestCase(Pages)).run(CompositionRemoteResult())

        self.assertTrue(any("second" in mark for mark in watch.marks), watch.marks)


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
