"""perf fails a run where a page it measures answers anything but 200, refuses a page that is none
or, asked to skip such pages, names each and measures the rest, and measures a page that redirects
as the page it lands on."""

import io
import subprocess
from contextlib import redirect_stdout
from unittest import mock

from django.test import SimpleTestCase, TestCase

from hq.platform.core.tests.browser.estate import Redirected, ask_as_production, landing, owner_client
from tools.perf import report
from tools.perf.tests.test_runs import PLAN, SET, SET_REFUSALS, Checks, FakeTarget, Measured


class AnsweredTests(Measured):
    def test_a_page_that_does_not_answer_200_fails_the_run_naming_the_page_and_its_status(self):
        # /hq/ is only walked for memory, so its redirect is no page measured.
        answers = {"/contacts/": 404, "/hq/": 302}

        def one_pass(_url, _cookie, pages):
            return {page: answers.get(page, 200) for page in pages}

        with mock.patch.object(report.measure, "one_pass", one_pass):
            kept, _ = self.measure(FakeTarget())
        with mock.patch.object(report, "ROOT", self.out):
            said = report.closing(kept, kept.doc["here"], None)

        self.assertEqual(
            [failure for failure in report.failures(kept.doc) if "answered" in failure],
            ["here page estate/contacts: /contacts/ answered 404, not 200"],
        )
        self.assertIn("/contacts/ answered 404", said)

    def beside_a_base_answering(self, base_answers: dict[str, int], here_answers: dict[str, int]):
        """A run beside a base whose first passes answer as given, every other page 200, and its closing line."""

        base = FakeTarget()
        base.url = "http://127.0.0.1:8"

        def one_pass(url, _cookie, pages):
            answers = base_answers if url == base.url else here_answers
            return {page: answers.get(page, 200) for page in pages}

        with mock.patch.object(report.measure, "one_pass", one_pass):
            kept, _ = self.measure(FakeTarget(), base=base)
        with mock.patch.object(report, "ROOT", self.out):
            said = report.closing(kept, kept.doc["here"], kept.doc["base"])
        return kept, said

    def test_a_page_the_base_answers_404_and_this_tree_serves_is_measured_here_alone_and_fails_nothing(self):
        kept, said = self.beside_a_base_answering({"/contacts/": 404}, {})

        self.assertEqual([failure for failure in report.failures(kept.doc) if "answered" in failure], [])
        self.assertEqual(self.server_times.pages, {"base": ["/"], "here": ["/", "/contacts/"]})
        contacts = kept.doc["here"]["pages"][1]["server"]
        self.assertEqual(contacts["p50_ms"], 6.0, "this tree measures the page")
        self.assertNotIn("moved_ms", contacts, "a page the base lacks has no move from it")
        self.assertNotIn("queries", kept.doc["base"]["pages"][1]["server"], "the base measures nothing of it")
        self.assertRegex(said, r"\bof 1 pages moved past their spread", "only the page on both sides is compared")
        self.assertRegex(said, r"estate/contacts[^;]*not at the base")

    def test_a_page_that_answers_404_on_both_sides_fails_on_both(self):
        kept, _ = self.beside_a_base_answering({"/contacts/": 404}, {"/contacts/": 404})

        self.assertEqual(
            [failure for failure in report.failures(kept.doc) if "answered" in failure],
            [
                "base page estate/contacts: /contacts/ answered 404, not 200",
                "here page estate/contacts: /contacts/ answered 404, not 200",
            ],
        )

    def test_a_base_page_that_answers_an_error_other_than_404_still_fails(self):
        kept, _ = self.beside_a_base_answering({"/contacts/": 500}, {})

        self.assertEqual(
            [failure for failure in report.failures(kept.doc) if "answered" in failure],
            ["base page estate/contacts: /contacts/ answered 500, not 200"],
        )


class AskedTests(TestCase):
    def test_a_page_that_is_none_is_refused_before_any_server_starts_naming_the_nearest_route(self):
        environment = {"usage_pages": "estate/containers", "usage_seed": "showcase", "usage_requests": "2"}
        with (
            mock.patch.dict("os.environ", environment),
            mock.patch.object(report, "measure_run") as measured,
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(report.main(), 2)
        measured.assert_not_called()
        self.assertIn("No page estate/containers", err.getvalue())
        self.assertIn("\n  estate/infrastructure/containers (the estate's control_plane:containers)", err.getvalue())

    def test_asked_to_skip_what_it_cannot_serve_it_names_each_such_page_and_measures_the_rest(self):
        environment = {
            "usage_pages": "template:control_plane/controller.html connections /infrastructure/containers/",
            "usage_skip_unservable": "true",
            "usage_seed": "showcase",
            "usage_requests": "2",
        }
        with mock.patch.dict("os.environ", environment), redirect_stdout(io.StringIO()) as said:
            asked = report.plan()

        self.assertEqual(asked.pages, ["/infrastructure/containers/"])
        for left_out in ("template:control_plane/controller.html", "connections"):
            self.assertIn(f"perf leaves out {left_out}", said.getvalue())

    def test_asked_to_skip_what_it_cannot_serve_when_it_serves_none_it_measures_nothing_and_passes(self):
        environment = {
            "usage_pages": "connections",
            "usage_skip_unservable": "true",
            "usage_seed": "showcase",
            "usage_requests": "2",
        }
        with (
            mock.patch.dict("os.environ", environment),
            mock.patch.object(report, "measure_run") as measured,
            redirect_stdout(io.StringIO()) as said,
        ):
            self.assertEqual(report.main(), 0)
        measured.assert_not_called()
        self.assertIn("perf leaves out connections", said.getvalue())

    def test_the_pages_asked_are_read_as_mise_hands_a_variadic_argument(self):
        environment = {
            "usage_pages": "/infrastructure/containers/ control_plane:containers 'estate/contacts'",
            "usage_seed": "showcase",
            "usage_requests": "2",
        }
        with mock.patch.dict("os.environ", environment):
            asked = report.plan()

        self.assertEqual(asked.pages, ["/infrastructure/containers/", "/contacts/"])

    def test_every_page_planned_answers_itself_never_a_redirect(self):
        environment = {"usage_pages": "estate/all", "usage_seed": "showcase", "usage_requests": "2"}
        with mock.patch.dict("os.environ", environment), redirect_stdout(io.StringIO()):
            asked = report.plan()

        with owner_client("showcase") as client:
            answered = {page: ask_as_production(client, page).status_code for page in asked.walked}
        self.assertEqual({page: status for page, status in answered.items() if status != 200}, {})

    def test_a_page_asked_that_redirects_is_measured_where_it_lands_and_said_so(self):
        environment = {"usage_pages": "estate/domains", "usage_seed": "showcase", "usage_requests": "2"}
        with mock.patch.dict("os.environ", environment), redirect_stdout(io.StringIO()) as said:
            asked = report.plan()

        (page,) = asked.pages
        self.assertNotEqual(page, "/domains/")
        self.assertTrue(page.startswith("/domains/"), page)
        self.assertIn(f"estate/domains lands on {report.estate_name(page)}", said.getvalue())


class EarlyRefusalTests(SimpleTestCase):
    """The task checks the set and the pages asked before it waits for the heavy turn, with no
    database: a test case that refuses every query proves it."""

    def refused(self, **asked) -> str:
        """What the early check prints for ``asked``, or "" where it lets the run go on."""

        environment = {"usage_seed": "showcase", **{f"usage_{key}": value for key, value in asked.items()}}
        with (
            mock.patch.dict("os.environ", environment),
            mock.patch("sys.stderr", new_callable=io.StringIO) as err,
            redirect_stdout(io.StringIO()) as out,
        ):
            try:
                report.refuse_early()
            except SystemExit as exited:
                self.assertEqual(exited.code, 2)
                return err.getvalue()
        self.assertEqual(out.getvalue(), "", "a page left out is the run's to name")
        return ""

    def test_a_page_that_is_none_is_refused_naming_the_nearest_route(self):
        said = self.refused(pages="control_plane:containrs")

        self.assertIn("No page control_plane:containrs", said)
        self.assertIn("\n  estate/infrastructure/containers (the estate's control_plane:containers)", said)

    def test_a_set_no_server_can_be_seeded_with_is_refused(self):
        self.assertIn("example-no-such-set", self.refused(pages="estate/all", seed="example-no-such-set"))

    def test_pages_perf_serves_or_is_asked_to_skip_go_on_to_the_run(self):
        for asked in (
            {"pages": "/infrastructure/containers/ control_plane:containers estate/all"},
            {"pages": "connections /infrastructure/containers/", "skip_unservable": "true"},
            {"pages": "connections", "skip_unservable": "true"},
        ):
            with self.subTest(**asked):
                self.assertEqual(self.refused(**asked), "")


class ComposedTests(Measured):
    """--image measures what production runs: the host image composed with the checkout's set."""

    def compose(self, returncode: int = 0, said: str = "") -> mock.MagicMock:
        done = subprocess.CompletedProcess([], returncode, stdout="", stderr=said)
        patcher = mock.patch.object(report.subprocess, "run", return_value=done)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def test_a_plan_with_an_extension_set_composes_each_tree_s_host_image_with_it_from_that_tree(self):
        composed = self.compose()
        tree = self.out / "base"
        with mock.patch.object(report, "run") as built:
            image = report.image_of(tree, "ba9876543210", here=False, composed=True)

        (build,) = (call.args[0] for call in built.call_args_list)
        (compose,) = (call.args[0] for call in composed.call_args_list)
        host = build[build.index("--tag") + 1]
        self.assertEqual(build[:3], ["mise", "-C", str(tree)])
        self.assertIn("image:compose", compose)
        self.assertEqual(compose[compose.index("--host") + 1], host)
        self.assertEqual(compose[compose.index("--context") + 1], str(tree))
        self.assertEqual(image, compose[compose.index("--tag") + 1])
        self.assertNotEqual(image, host)

    def test_the_host_alone_is_its_image_as_built(self):
        composed = self.compose()
        with mock.patch.object(report, "run"):
            image = report.image_of(self.out, "ba9876543210", here=False, composed=False)

        composed.assert_not_called()
        self.assertEqual(image, "severino-hq:perf-ba9876543210")

    def test_a_composition_its_tree_cannot_load_starts_the_run_again_alone(self):
        # The build's own report of the failure buries the refusal; the tree's check names it.
        build_tail = "".join(f" {line} | RUN collectstatic\n" for line in range(40))
        failed = subprocess.CompletedProcess([], 1, stdout="", stderr=build_tail + "ERROR: failed to solve\n")
        checks = Checks(composed=SET_REFUSALS[0])

        def ran(command, **kwargs):
            return failed if "image:compose" in command else checks(command, **kwargs)

        asked = report.Plan(**{**PLAN.__dict__, "image": True, "extensions": [SET]})
        with (
            mock.patch.dict("os.environ", {"HQ_LOCAL_PLUGINS": SET}),
            mock.patch.object(report.subprocess, "run", ran),
            mock.patch.object(report, "run"),
            self.assertRaisesRegex(report.SetRefused, r"requires API 5"),
        ):
            self.measure(FakeTarget(), asked)

    def test_a_composed_image_is_kept_apart_from_the_host_image_s(self):
        image = report.Plan(**{**PLAN.__dict__, "image": True})
        composed = report.Plan(**{**image.__dict__, "extensions": ["example.ledger.plugin:plugin"]})

        self.assertEqual((image.target, composed.target), ("image", "image:composed"))


class Answer:
    def __init__(self, status: int, location: str = "") -> None:
        self.status_code, self.headers = status, {"Location": location}


class LandingTests(SimpleTestCase):
    def test_a_redirect_within_hq_is_followed_to_the_page_that_answers(self):
        answers = {"/a/": Answer(302, "/b/?x=1"), "/b/?x=1": Answer(301, "c/"), "/b/c/": Answer(200)}
        self.assertEqual(landing(answers.__getitem__, "/a/"), "/b/c/")

    def test_a_redirect_out_of_hq_or_in_a_loop_lands_on_no_page(self):
        cases = {
            "out": {"/a/": Answer(302, "https://example.com/")},
            "loop": {"/a/": Answer(302, "/b/"), "/b/": Answer(302, "/a/")},
        }
        for case, answers in cases.items():
            with self.subTest(case), self.assertRaises(Redirected):
                landing(answers.__getitem__, "/a/")
