"""A CodeQL analysis passes only when it ran and left no alert the config keeps."""

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


@tag("source")
class CodeQLReportTests(SimpleTestCase):
    """scan:codeql's jq filter, run as the task runs it, excluded rules as `--args`."""

    def judge(self, data, *rules) -> int:
        jq = shutil.which("jq")
        self.assertTrue(jq, "mise.toml pins jq")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codeql.sarif"
            path.write_text(json.dumps(data))
            command = [str(jq), "-r", "-f", str(REPORT), str(path), "--args", *rules]
            return subprocess.run(command, capture_output=True, check=False).returncode

    def test_a_run_with_no_results_passes(self):
        self.assertEqual(self.judge(sarif(run())), 0)

    def test_an_analysis_with_no_run_fails(self):
        for data in (sarif(), {"version": "2.1.0"}):
            with self.subTest(data=data):
                self.assertNotEqual(self.judge(data), 0)

    def test_an_alert_fails(self):
        self.assertNotEqual(self.judge(sarif(run(result("py/sql-injection")))), 0)

    def test_an_alert_on_an_excluded_rule_passes(self):
        self.assertEqual(self.judge(sarif(run(result("py/unused-import"))), "py/unused-import"), 0)

    def test_only_the_excluded_rules_drop(self):
        alerts = sarif(run(result("py/unused-import")), run(result("py/sql-injection")))
        self.assertNotEqual(self.judge(alerts, "py/unused-import", "py/cyclic-import"), 0)
        self.assertEqual(self.judge(alerts, "py/unused-import", "py/sql-injection"), 0)
