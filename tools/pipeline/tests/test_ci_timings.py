"""audit:ci-timings hands gh nothing but run ids, so its unquoted list cannot reach gh as a flag."""

import io
from unittest import mock

from django.test import SimpleTestCase

from tools.pipeline import ci_timings


class RunWordTests(SimpleTestCase):
    def test_a_word_that_is_not_a_run_id_is_refused_before_gh_runs(self):
        for words in (["--web"], ["123", "-R"], ["12a"], ["١٢"], []):
            with (
                self.subTest(words=words),
                mock.patch.object(ci_timings.subprocess, "run") as run,
                mock.patch("sys.stderr", io.StringIO()) as said,
            ):
                self.assertEqual(ci_timings.main(words), 2)
                run.assert_not_called()
                self.assertIn("ci_timings", said.getvalue())
