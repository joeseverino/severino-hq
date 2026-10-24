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

from hq.platform.core import seeds
from tools.perf import report, runs, targets
from tools.perf.targets import NotMeasured

PLAN = report.Plan(
    pages=["/", "/contacts/"],
    walked=["/", "/contacts/", "/hq/"],
    viewports=[{"width": 402, "height": 874}],
    requests=2,
    image=False,
    seed="showcase",
    extensions=[],
)


class FakeTarget:
    """A started server that answers every measurement, and fails to stop when asked to."""

    url = "http://127.0.0.1:9"
    measured = "a fake's memory"

    def __init__(self, *_args, stop_fails: bool = False, **_kwargs) -> None:
        self.stop_fails = stop_fails
        self.asked = []
        self.stopped = False

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.stopped = True
        if self.stop_fails:
            raise RuntimeError("the server outlived its stop")

    def session(self):
        return ("sessionid", "k3y")

    def memory(self):
        self.asked.append("memory")
        return 100 * 1024**2

    def queries(self, pages):
        return dict.fromkeys(pages, 7)

    def cold_start(self):
        self.asked.append("cold start")
        return 1500.0


class ServerTimes:
    """Every server answering 4 ms each round, and here 6 ms beside a base; each call's servers
    kept, and what each target had been asked before it."""

    def __init__(self) -> None:
        self.asked: list[list[str]] = []
        self.targets: list[FakeTarget] = []
        self.before: list[list[str]] = []

    def __call__(self, servers, pages, rounds):
        self.asked.append(list(servers))
        self.before = [step for target in self.targets for step in target.asked]
        ms = {"base": 4.0, "here": 6.0 if "base" in servers else 4.0}
        return {
            side: {
                page: {"status": 200, "p50_ms": ms[side], "p95_ms": 5.0, "times_ms": [ms[side]] * rounds}
                for page in pages
            }
            for side in servers
        }


def no_chromium():
    raise RuntimeError("Executable doesn't exist at /nowhere/chromium")


class Measured(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)
        self.passed = []
        self.server_times = ServerTimes()

        def one_pass(_url, _cookie, pages):
            self.passed.append(pages)
            return dict.fromkeys(pages, 200)

        for patcher in (
            mock.patch.object(report, "OUT", self.out),
            mock.patch.object(report.measure, "one_pass", one_pass),
            mock.patch.object(report.measure, "server_times", self.server_times),
            mock.patch.object(report, "chromium", no_chromium),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def measure(
        self, target: FakeTarget, asked: report.Plan = PLAN, log: str = "", base: FakeTarget | None = None
    ) -> tuple[runs.Run, list[dict]]:
        """A run measuring ``target`` as its here side, beside ``base`` when given, its server's log
        saying ``log``, and the document as each save left it."""

        kept = runs.Run.start(self.out, "here-dev-scratch", plan=asked.key())
        (self.out / f"{kept.name}.here.log").write_text(log, encoding="utf-8")
        self.server_times.targets = [base, target] if base else [target]
        served = iter(self.server_times.targets)
        saves = []
        save = kept.save

        def snapshot():
            save()
            saves.append(json.loads(kept.path.read_text(encoding="utf-8")))

        kept.save = snapshot
        with (
            mock.patch.object(report, "Scratch", lambda *_args, **_kwargs: next(served)),
            mock.patch.object(report, "base_tree", lambda _commit: contextlib.nullcontext(self.out)),
            mock.patch.object(report, "ROOT", self.out),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            report.measure_run("ba9876543210" * 3 if base else None, "0123456789ab", asked, kept, fresh=False)
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

    def test_only_the_pages_asked_are_measured_and_memory_is_read_after_a_pass_over_every_page(self):
        kept, _ = self.measure(FakeTarget())
        here = kept.doc["here"]

        self.assertEqual([entry["page"] for entry in here["pages"]], PLAN.pages)
        self.assertTrue(self.passed)
        self.assertTrue(all(walked == PLAN.walked for walked in self.passed))

    def test_memory_is_read_on_a_server_started_again_from_what_its_first_boot_compiled(self):
        target = FakeTarget()
        self.measure(target)

        self.assertEqual(target.asked[0], "cold start")
        self.assertIn("memory", target.asked)

    def test_each_measurement_is_saved_as_it_finishes(self):
        _, saves = self.measure(FakeTarget())

        timed = [doc for doc in saves if doc["here"]["pages"][0]["server"].get("p50_ms")]
        self.assertTrue(timed)
        self.assertNotIn(
            "queries", timed[0]["here"]["pages"][0]["server"], "server times reach the file before the run goes on"
        )

    def test_a_server_that_fails_to_stop_keeps_what_it_served(self):
        kept, _ = self.measure(FakeTarget(stop_fails=True))
        here = json.loads(kept.path.read_text(encoding="utf-8"))["here"]

        self.assertIn("stop", [failure["step"] for failure in here["failures"]])
        self.assertEqual(here["pages"][1]["server"]["p95_ms"], 5.0)

    def test_a_server_that_will_not_start_is_recorded(self):
        kept, _ = self.measure(Refusing())
        here = json.loads(kept.path.read_text(encoding="utf-8"))["here"]

        self.assertEqual(here["failures"][0]["step"], "start")
        self.assertIn("exited before it was ready", here["failures"][0]["error"])

    def test_the_closing_line_names_each_failure_and_where_the_run_is(self):
        kept, _ = self.measure(FakeTarget())
        with mock.patch.object(report, "ROOT", self.out):
            said = report.closing(kept, kept.doc["here"], None)

        self.assertIn("here browser:", said)
        self.assertIn(kept.path.name, said)


# The tail of a scratch server's log when an extension needs a newer plugin API than the ref's HQ.
REFUSED_LOG = """\
Operations to perform:
  File "/tmp/checkout/hq/platform/application/plugins.py", line 141, in _validate_manifest
    raise ImproperlyConfigured(
django.core.exceptions.ImproperlyConfigured: Plugin 'example.ledger' requires API 5; HQ supports 4.
"""


class Refusing(FakeTarget):
    def __enter__(self):
        raise NotMeasured("the server at http://127.0.0.1:9 exited before it was ready")


class BesideABaseTests(Measured):
    def test_both_servers_are_up_together_and_asked_for_server_time_in_one_call(self):
        base, here = FakeTarget(), FakeTarget()

        kept, _ = self.measure(here, base=base)

        self.assertEqual(self.server_times.asked, [["base", "here"]])
        self.assertTrue(base.stopped and here.stopped)
        server = kept.doc["here"]["pages"][0]["server"]
        self.assertEqual((server["moved_ms"], server["spread_ms"]), (2.0, 0.0))
        self.assertEqual(kept.doc["base"]["pages"][0]["server"]["p50_ms"], 4.0)

    def test_server_time_is_asked_while_both_servers_are_as_their_first_boot_left_them(self):
        base, here = FakeTarget(), FakeTarget()

        self.measure(here, base=base)

        self.assertEqual(self.server_times.before, [], "no cold start or memory read before server time")
        self.assertIn("cold start", base.asked)
        self.assertIn("cold start", here.asked)

    def test_a_kept_base_stands_in_for_all_but_server_time_which_is_asked_of_its_server_again(self):
        seen = {"first": {}, "repeat": {}, "script_ms": 1.0, "long_tasks": 0, "long_task_ms": 0, "cls": 0}
        for patcher in (
            mock.patch.object(report, "chromium", contextlib.nullcontext),
            mock.patch.object(
                report.measure, "browser_costs", lambda _b, _u, _c, pages, _v: {page: {"402": seen} for page in pages}
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        previous, _ = self.measure(FakeTarget(), base=FakeTarget())
        base = FakeTarget()
        kept_before = previous.doc["base"]["cold_start"]

        kept, _ = self.measure(FakeTarget(), base=base)

        self.assertEqual(kept.doc["base_reused_from"], previous.name)
        self.assertNotIn("cold start", base.asked, "the kept report's cold start stands")
        self.assertEqual(kept.doc["base"]["cold_start"], kept_before)
        self.assertEqual(self.server_times.asked[-1], ["base", "here"])

    def test_a_base_that_will_not_start_leaves_here_measured_alone(self):
        here = FakeTarget()

        kept, _ = self.measure(here, base=Refusing())

        self.assertEqual(kept.doc["base"]["failures"][0]["step"], "start")
        self.assertEqual(self.server_times.asked, [["here"]])
        self.assertNotIn("moved_ms", kept.doc["here"]["pages"][0]["server"])

    def test_the_closing_line_counts_and_names_the_pages_that_moved_past_their_spread(self):
        kept, _ = self.measure(FakeTarget(), base=FakeTarget())
        kept.doc["here"]["pages"][1]["server"]["spread_ms"] = 9.0
        with mock.patch.object(report, "ROOT", self.out):
            said = report.closing(kept, kept.doc["here"], kept.doc["base"])

        self.assertIn("1 of 2 pages moved past their spread (/)", said)
        self.assertIn("showcase", said)


class HostAloneTests(Measured):
    def test_a_server_refusing_the_extension_set_for_its_plugin_api_starts_the_run_again_alone(self):
        with self.assertRaisesRegex(report.SetRefused, r"requires API 5; HQ supports 4\."):
            self.measure(Refusing(), log=REFUSED_LOG)

    def test_any_other_start_failure_is_this_side_s_alone(self):
        kept, _ = self.measure(Refusing(), log="OSError: [Errno 48] Address already in use\n")

        self.assertEqual(kept.doc["here"]["failures"][0]["step"], "start")

    def test_measured_alone_a_refusal_is_a_failure_and_never_a_loop(self):
        alone = report.Plan(**{**PLAN.__dict__, "alone": "--host-alone asked for it"})

        kept, _ = self.measure(Refusing(), alone, log=REFUSED_LOG)

        self.assertEqual(kept.doc["here"]["failures"][0]["step"], "start")

    def test_the_closing_line_says_the_host_was_measured_alone_and_why(self):
        kept, _ = self.measure(FakeTarget())
        kept.doc["alone"] = (
            "ba9876543210 refused the extension set: Plugin 'example.ledger' requires API 5; HQ supports 4."
        )
        with mock.patch.object(report, "ROOT", self.out):
            said = report.closing(kept, kept.doc["here"], kept.doc["here"])

        self.assertIn("host measured alone on both sides because ba9876543210 refused", said)

    def test_alone_a_scratch_server_reads_no_extension_set(self):
        tree = Path("/tmp/checkout")
        with mock.patch.dict(
            "os.environ", {"HQ_LOCAL_PLUGINS": "example.ledger.plugin:plugin", "HQ_LOCAL_PYTHONPATH": "/x"}
        ):
            composed = targets.scratch_env(tree, trusted=True, alone=False)
            alone = targets.scratch_env(tree, trusted=True, alone=True)

        self.assertEqual(composed["HQ_LOCAL_PLUGINS"], "example.ledger.plugin:plugin")
        self.assertNotIn("HQ_LOCAL_PLUGINS", alone)
        self.assertNotIn("HQ_LOCAL_PYTHONPATH", alone)
        self.assertEqual(alone["MISE_IGNORED_CONFIG_PATHS"], str(tree / "mise.local.toml"))


class KeptTests(SimpleTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.out = Path(folder.name)

    def kept(self, commit: str, plan: dict, *, failures=(), finished="2026-10-24T10:00:00+00:00") -> runs.Run:
        kept = runs.Run.start(self.out, f"{commit}-dev-scratch", plan=plan)
        side = runs.side(commit, plan)
        pages = [{"page": page, "server": {"p50_ms": 3.0 + at}} for at, page in enumerate(plan["pages"])]
        side.update(failures=list(failures), finished_at=finished, pages=pages)
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

    def test_a_report_of_another_data_set_stands_in_for_nothing(self):
        self.kept("0123456789ab", PLAN.key())

        self.assertIsNone(runs.saved(self.out, "0123456789ab", PLAN.key() | {"seed": "scale"}))

    def test_another_plan_a_failure_or_an_unfinished_run_stands_in_for_nothing(self):
        other = copy.deepcopy(PLAN.key()) | {"requests": 20}
        self.kept("0123456789ab", other)
        self.kept("0123456789ab", PLAN.key(), failures=[{"step": "browser", "error": "x"}])
        self.kept("0123456789ab", PLAN.key(), finished=None)

        self.assertIsNone(runs.saved(self.out, "0123456789ab", PLAN.key()))
        self.assertIsNone(runs.saved(self.out, "ba9876543210", other))

    def test_a_report_of_more_pages_stands_in_for_any_of_them_holding_just_those(self):
        every = PLAN.key() | {"pages": ["/", "/hq/", "/contacts/"]}
        self.kept("0123456789ab", every)
        one = PLAN.key() | {"pages": ["/contacts/"]}

        source, found = runs.saved(self.out, "0123456789ab", one)

        self.assertEqual([entry["page"] for entry in found["pages"]], ["/contacts/"])
        self.assertEqual(found["pages"][0]["server"]["p50_ms"], 5.0)
        self.assertEqual(found["plan"], one, "kept again, it stands in for the pages it holds and no more")
        self.assertTrue(source)

    def test_a_report_of_fewer_pages_stands_in_for_nothing_more(self):
        self.kept("0123456789ab", PLAN.key() | {"pages": ["/contacts/"]})

        self.assertIsNone(runs.saved(self.out, "0123456789ab", PLAN.key()))

    def test_a_report_whose_browser_waited_otherwise_stands_in_for_nothing(self):
        self.kept("0123456789ab", PLAN.key() | {"browser_waits_for": "networkidle"})

        self.assertIsNone(runs.saved(self.out, "0123456789ab", PLAN.key()))


class SeedTests(SimpleTestCase):
    def test_a_set_a_server_can_be_seeded_with_is_taken(self):
        for name in seeds.SEEDS:
            with self.subTest(name=name):
                self.assertEqual(report.seed_of(name), name)

    def test_a_set_no_server_can_be_seeded_with_is_refused_with_those_it_can(self):
        with (
            mock.patch.dict(seeds.SEEDS, {"scale": seeds.SEEDS["scale"]}, clear=True),
            self.assertRaisesRegex(report.Unservable, r"No data set 'dense'.*Sets: scale$"),
        ):
            report.seed_of("dense")

    def test_a_scratch_server_is_seeded_with_the_set_asked(self):
        started = []

        def popen(command, **_kwargs):
            started.append(command)
            return mock.Mock(poll=lambda: None, pid=0)

        with (
            mock.patch.object(targets.subprocess, "Popen", popen),
            mock.patch.object(targets, "wait_ready", lambda *_args: 0.0),
            mock.patch.object(targets, "stop_group", lambda _process: None),
            tempfile.TemporaryDirectory() as folder,
            targets.Scratch(Path(folder), Path(folder) / "log", "scale"),
        ):
            pass

        self.assertEqual(started[0][started[0].index("--seed") + 1], "scale")
