"""perf fails a run where a page it measures answers anything but 200, refuses a page that is none,
and measures a page that redirects as the page it lands on."""

import io
from contextlib import redirect_stdout
from unittest import mock

from django.test import SimpleTestCase, TestCase

from hq.platform.core.tests.browser.estate import Redirected, ask_as_production, landing, owner_client
from tools.perf import report
from tools.perf.tests.test_runs import FakeTarget, Measured


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
        self.assertIn("control_plane:containers (/infrastructure/containers/)", err.getvalue())

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
