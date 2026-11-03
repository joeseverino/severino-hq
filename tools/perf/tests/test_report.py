"""perf fails a run where a page it measures answers anything but 200, refuses a page that is none,
and measures a page that redirects as the page it lands on."""

import io
import subprocess
from contextlib import redirect_stdout
from unittest import mock

from django.test import SimpleTestCase, TestCase

from hq.platform.core.tests.browser.estate import Redirected, ask_as_production, landing, owner_client
from tools.perf import report
from tools.perf.tests.test_runs import PLAN, REFUSED_LOG, FakeTarget, Measured


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

    def test_a_composition_refused_for_its_plugin_api_starts_the_run_again_alone(self):
        # The refusal is the collect step's, with the build's own report of the failure after it.
        build_tail = "".join(f" {line} | RUN collectstatic\n" for line in range(40))
        self.compose(1, said=REFUSED_LOG + build_tail + "ERROR: failed to solve: exit code: 1\n")
        asked = report.Plan(**{**PLAN.__dict__, "image": True, "extensions": ["example.ledger.plugin:plugin"]})

        with mock.patch.object(report, "run"), self.assertRaisesRegex(report.SetRefused, r"requires API 5"):
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
