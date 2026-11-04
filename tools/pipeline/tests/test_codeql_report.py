"""A CodeQL analysis passes only when it ran and left no alert, and GitHub holds none open."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, tag

REPORT = Path(__file__).resolve().parents[1] / "codeql_report.jq"


def result(rule):
    """One SARIF 2.1.0 result, as `codeql database analyze --format=sarif-latest` writes it."""
    return {
        "ruleId": rule,
        "rule": {"id": rule, "index": 0},
        "message": {"text": "Something the query found."},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": "hq/example.py", "uriBaseId": "%SRCROOT%", "index": 0},
                    "region": {"startLine": 12, "startColumn": 5, "endColumn": 20},
                }
            }
        ],
    }


def sarif(*runs):
    return {"$schema": "https://json.schemastore.org/sarif-2.1.0.json", "version": "2.1.0", "runs": list(runs)}


def run(*results):
    return {"tool": {"driver": {"name": "CodeQL", "rules": []}}, "results": list(results)}


def alert(rule, state="open"):
    """One alert of GitHub's code-scanning alerts answer, in the shape of a recorded one."""
    return {
        "number": 2055,
        "state": state,
        "rule": {"id": rule},
        "tool": {"name": "CodeQL"},
        "most_recent_instance": {
            "ref": "refs/pull/1/merge",
            "state": state,
            "location": {
                "path": "hq/example.py",
                "start_line": 44,
                "end_line": 44,
                "start_column": 18,
                "end_column": 26,
            },
            "message": {"text": "Variable _closing is not used."},
        },
    }


@tag("source")
class Report(SimpleTestCase):
    """The jq filter both scan tasks end with, run as they run it."""

    def judge(self, data) -> int:
        return self.report(data).returncode

    def report(self, data) -> subprocess.CompletedProcess:
        jq = shutil.which("jq")
        self.assertTrue(jq, "mise.toml pins jq")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codeql.sarif"
            path.write_text(json.dumps(data))
            command = [str(jq), "-r", "-f", str(REPORT), str(path)]
            return subprocess.run(command, capture_output=True, text=True, check=False)


@tag("source")
class CodeQLReportTests(Report):
    """scan:codeql's judge of a SARIF file."""

    def test_a_run_with_no_results_passes(self):
        self.assertEqual(self.judge(sarif(run())), 0)

    def test_an_analysis_with_no_run_fails(self):
        for data in (sarif(), {"version": "2.1.0"}):
            with self.subTest(data=data):
                self.assertNotEqual(self.judge(data), 0)

    def test_an_alert_fails(self):
        self.assertNotEqual(self.judge(sarif(run(result("py/sql-injection")))), 0)

    def test_an_alert_in_any_run_fails(self):
        self.assertNotEqual(self.judge(sarif(run(), run(result("py/unused-import")))), 0)


@tag("source")
class OpenAlertsReportTests(Report):
    """scan:alerts' read of the alerts GitHub holds, through the same filter."""

    def test_no_open_alert_passes(self):
        for data in ([], [[]], [[], []]):
            with self.subTest(data=data):
                self.assertEqual(self.judge(data), 0)

    def test_an_open_alert_fails_naming_it(self):
        for data in ([alert("py/unused-local-variable")], [[alert("py/unused-local-variable")]]):
            with self.subTest(data=data):
                report = self.report(data)
                self.assertNotEqual(report.returncode, 0)
                self.assertIn("py/unused-local-variable", report.stdout)
                self.assertIn("hq/example.py:44", report.stdout)

    def test_an_open_alert_on_a_later_page_fails(self):
        self.assertNotEqual(self.judge([[alert("py/unused-import", "dismissed")], [alert("py/unused-import")]]), 0)

    def test_a_dismissed_alert_passes(self):
        for state in ("dismissed", "fixed"):
            with self.subTest(state=state):
                self.assertEqual(self.judge([[alert("py/unnecessary-lambda", state)]]), 0)
