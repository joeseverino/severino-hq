"""A local Scorecard run passes only when every file-based check scores 10."""

import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from django.test import SimpleTestCase

from tools.pipeline import scorecard_report


def check(name, score=10, details=None, reason="reason"):
    """One entry of `scorecard --show-details --format json`'s checks, as v5 writes it."""
    return {
        "details": details,
        "score": score,
        "reason": reason,
        "name": name,
        "documentation": {"url": "https://example.com/checks.md", "short": "What the check determines."},
    }


def report(*checks):
    return {
        "date": "2026-10-23T14:52:24-05:00",
        "repo": {"name": "file:///src", "commit": "unknown"},
        "scorecard": {"version": "v5.6.0", "commit": "0" * 40},
        "score": 10.0,
        "checks": list(checks),
        "metadata": None,
    }


def every_check(**replaced):
    return report(*(replaced.get(name) or check(name) for name in scorecard_report.CHECKS))


class ScorecardReportTests(SimpleTestCase):
    def judge(self, data) -> tuple[int, str]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scorecard.json"
            path.write_text(json.dumps(data))
            out = StringIO()
            with redirect_stdout(out), redirect_stderr(StringIO()):
                code = scorecard_report.main(str(path))
        return code, out.getvalue()

    def test_every_check_at_ten_passes(self):
        self.assertEqual(self.judge(every_check())[0], 0)

    def test_a_check_below_ten_fails_with_or_without_a_warning(self):
        for details in (None, ["Info: found something"], ["Warn: missing something"]):
            with self.subTest(details=details):
                code, out = self.judge(
                    every_check(**{"Security-Policy": check("Security-Policy", 3, details, "policy incomplete")})
                )
                self.assertEqual(code, 1)
                self.assertIn("Security-Policy: 3/10", out)

    def test_an_inconclusive_check_fails(self):
        self.assertEqual(self.judge(every_check(Packaging=check("Packaging", -1)))[0], 1)

    def test_the_closing_line_names_each_failed_check_and_the_output_says_why(self):
        # Token-Permissions scores 10 and warns on a job-level write, as v5.6.0 writes it.
        warned = "Warn: jobLevel 'contents' permission set to 'write': .github/workflows/example.yml:12"
        code, out = self.judge(
            every_check(
                Packaging=check("Packaging", -1, None, "internal error: no network"),
                **{"Token-Permissions": check("Token-Permissions", 10, ["Info: read", warned])},
            )
        )
        self.assertEqual(code, 1)
        closing = out.splitlines()[-1]
        self.assertIn("Packaging", closing)
        self.assertIn("Token-Permissions", closing)
        self.assertNotIn("SAST", closing)
        self.assertIn("internal error: no network", out)
        self.assertIn(warned, out)

    def test_a_passing_run_closes_on_its_verdict(self):
        code, out = self.judge(every_check())
        self.assertEqual(code, 0)
        self.assertIn("Scorecard", out.splitlines()[-1])

    def test_a_warning_beside_a_ten_is_informational_except_in_a_strict_check(self):
        warned = ["Info: detected", "Warn: no pull requests merged into dev branch"]
        self.assertEqual(self.judge(every_check(SAST=check("SAST", 10, warned)))[0], 0)
        for name in scorecard_report.STRICT:
            with self.subTest(name=name):
                self.assertEqual(self.judge(every_check(**{name: check(name, 10, warned)}))[0], 1)

    def test_a_check_the_run_did_not_report_fails(self):
        code, out = self.judge(report(*(check(name) for name in scorecard_report.CHECKS[1:])))
        self.assertEqual(code, 1)
        self.assertIn(f"{scorecard_report.CHECKS[0]}: not reported", out)
