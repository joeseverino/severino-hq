"""A CodeQL analysis passes only when it ran and left no alert the config keeps."""

import json
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from django.test import SimpleTestCase

from tools.pipeline import codeql_report


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


class CodeQLReportTests(SimpleTestCase):
    def judge(self, data, *argv) -> int:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codeql.sarif"
            path.write_text(json.dumps(data))
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                return codeql_report.main([str(path), *argv])

    def test_a_run_with_no_results_passes(self):
        self.assertEqual(self.judge(sarif(run())), 0)

    def test_an_analysis_with_no_run_fails(self):
        for data in (sarif(), {"version": "2.1.0"}):
            with self.subTest(data=data):
                self.assertEqual(self.judge(data), 1)

    def test_an_alert_fails(self):
        self.assertEqual(self.judge(sarif(run(result("py/sql-injection")))), 1)

    def test_an_alert_on_an_excluded_rule_passes(self):
        self.assertEqual(self.judge(sarif(run(result("py/unused-import"))), "--exclude", "py/unused-import"), 0)
