"""perf:report keeps what it measured: each run in a file of its own, saved as each
measurement finishes, a failure recorded beside the numbers that did not fail."""

import contextlib
import copy
import io
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from tools.perf import report, runs
from tools.perf.targets import NotMeasured

PLAN = report.Plan(
    pages=["/", "/contacts/"],
    viewports=[{"width": 402, "height": 874}],
    requests=2,
    image=False,
    extensions=[],
)


class FakeTarget:
    """A started server that answers every measurement, and fails to stop when asked to."""

    url = "http://127.0.0.1:9"
    measured = "a fake's memory"

    def __init__(self, *_args, stop_fails: bool = False, **_kwargs) -> None:
        self.stop_fails = stop_fails

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        if self.stop_fails:
            raise RuntimeError("the server outlived its stop")

    def session(self):
        return ("sessionid", "k3y")

    def memory(self):
        return 100 * 1024**2

    def queries(self, pages):
        return dict.fromkeys(pages, 7)

    def cold_start(self):
        return 1500.0


def server_times(_url, _cookie, pages, _requests):
    return {page: {"status": 200, "p50_ms": 4.0, "p95_ms": 5.0} for page in pages}


def no_chromium():
    raise RuntimeError("Executable doesn't exist at /nowhere/chromium")


class Measured(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)
        for patcher in (
            mock.patch.object(report, "OUT", self.out),
            mock.patch.object(report.measure, "one_pass", lambda _url, _cookie, pages: dict.fromkeys(pages, 200)),
            mock.patch.object(report.measure, "server_times", server_times),
            mock.patch.object(report, "chromium", no_chromium),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def measure(self, target: FakeTarget) -> tuple[runs.Run, list[dict]]:
        """A run measuring ``target`` as its here side, and the document as each save left it."""

        kept = runs.Run.start(self.out, "here-dev-scratch", plan=PLAN.key())
        saves = []
        save = kept.save

        def snapshot():
            save()
            saves.append(json.loads(kept.path.read_text(encoding="utf-8")))

        kept.save = snapshot
        with (
            mock.patch.object(report, "Scratch", lambda *_args, **_kwargs: target),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report.measure_tree(self.out, "0123456789ab", True, PLAN, kept, "here")
        return kept, saves


class FailureTests(Measured):
    def test_a_failed_measurement_is_recorded_and_the_rest_are_measured(self):
        kept, _ = self.measure(FakeTarget())
        here = json.loads(kept.path.read_text(encoding="utf-8"))["here"]

        self.assertEqual([failure["step"] for failure in here["failures"]], ["browser"])
        self.assertIn("chromium", here["failures"][0]["error"])
        self.assertEqual(here["pages"][0]["server"]["p50_ms"], 4.0)
        self.assertEqual(here["pages"][0]["server"]["queries"], 7)
        self.assertEqual(here["cold_start"]["median_ms"], 1500.0)
        self.assertTrue(here["finished_at"])

    def test_each_measurement_is_saved_as_it_finishes(self):
        _, saves = self.measure(FakeTarget())

        server_saved = [
            doc for doc in saves if doc["here"]["pages"] and doc["here"]["pages"][0]["server"].get("p50_ms")
        ]
        self.assertTrue(server_saved)
        self.assertNotIn("cold_start", server_saved[0]["here"], "server times reach the file before the run goes on")

    def test_a_server_that_fails_to_stop_keeps_what_it_served(self):
        kept, _ = self.measure(FakeTarget(stop_fails=True))
        here = json.loads(kept.path.read_text(encoding="utf-8"))["here"]

        self.assertIn("stop", [failure["step"] for failure in here["failures"]])
        self.assertEqual(here["pages"][1]["server"]["p95_ms"], 5.0)

    def test_a_server_that_will_not_start_is_recorded(self):
        class Refusing(FakeTarget):
            def __enter__(self):
                raise NotMeasured("the server exited before it was ready")

        kept, _ = self.measure(Refusing())
        here = json.loads(kept.path.read_text(encoding="utf-8"))["here"]

        self.assertEqual(here["failures"], [{"step": "start", "error": "the server exited before it was ready"}])

    def test_the_closing_line_names_each_failure_and_where_the_run_is(self):
        kept, _ = self.measure(FakeTarget())
        with mock.patch.object(report, "ROOT", self.out):
            said = report.closing(kept, kept.doc["here"], None)

        self.assertIn("here browser:", said)
        self.assertIn(kept.path.name, said)


class KeptTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)

    def kept(self, commit: str, plan: dict, *, failures=(), finished="2026-10-24T10:00:00+00:00") -> runs.Run:
        kept = runs.Run.start(self.out, f"{commit}-dev-scratch", plan=plan)
        side = runs.side(commit, plan)
        side.update(failures=list(failures), finished_at=finished, pages=[{"page": "/", "server": {"p50_ms": 3.0}}])
        kept.doc["here"] = side
        kept.save()
        return kept

    def test_a_run_never_overwrites_another_of_the_same_commit_and_target(self):
        at = datetime(2026, 10, 24, 12, tzinfo=UTC)
        stem = runs.name("0123456789ab", "ba9876543210", "dev:scratch", at)
        first = runs.Run.start(self.out, stem)
        second = runs.Run.start(self.out, stem)

        self.assertNotEqual(first.path, second.path)
        self.assertTrue(first.path.is_file())
        self.assertIn("0123456789ab-vs-ba9876543210-dev-scratch", stem)

    def test_a_complete_report_of_the_commit_under_the_same_plan_is_found(self):
        self.kept("0123456789ab", PLAN.key())

        found = runs.saved(self.out, "0123456789ab", PLAN.key())

        self.assertIsNotNone(found)
        self.assertEqual(found[1]["pages"][0]["server"]["p50_ms"], 3.0)

    def test_another_plan_a_failure_or_an_unfinished_run_stands_in_for_nothing(self):
        other = copy.deepcopy(PLAN.key()) | {"requests": 20}
        self.kept("0123456789ab", other)
        self.kept("0123456789ab", PLAN.key(), failures=[{"step": "browser", "error": "x"}])
        self.kept("0123456789ab", PLAN.key(), finished=None)

        self.assertIsNone(runs.saved(self.out, "0123456789ab", PLAN.key()))
        self.assertIsNone(runs.saved(self.out, "ba9876543210", other))
