"""perf:report fails a run where a page it measures answers anything but 200, and refuses a page that is none."""

import io
from contextlib import redirect_stdout
from unittest import mock

from django.test import SimpleTestCase

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


class AskedTests(SimpleTestCase):
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
