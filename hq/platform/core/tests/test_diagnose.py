"""The pipeline's plain-English diagnoses (scripts/diagnose.py)."""

import importlib.util
import json
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]


def _diagnose():
    spec = importlib.util.spec_from_file_location("diagnose", ROOT / "scripts" / "diagnose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiagnoseTests(SimpleTestCase):
    def test_a_known_failure_is_named_with_its_fix(self):
        found = _diagnose().diagnose(
            "#15 pushing\\ndenied: permission_denied: write_package\\n##[error]Process completed"
        )

        self.assertEqual(found["id"], "package-write")
        self.assertIn("Write role", found["fix"])

    def test_a_rolled_back_deploy_says_production_is_unchanged(self):
        found = _diagnose().diagnose("health: starting\\nNew image did not become healthy.\\nRestoring previous image")

        self.assertEqual(found["id"], "unhealthy")
        self.assertIn("previous image", found["fix"])

    def test_a_controller_that_could_not_use_a_connection_says_where_to_look(self):
        found = _diagnose().diagnose(
            "Controller connection preflight failed; inspect the private host log.\n"
            "Controller activation failed; rolling back application image."
        )

        self.assertEqual(found["id"], "controller-preflight")
        self.assertIn("controller-preflight.log", found["fix"])

    def test_nothing_known_says_so_and_asks_for_an_entry(self):
        found = _diagnose().diagnose("something nobody has seen")

        self.assertEqual(found["id"], "unknown")
        self.assertIn("deploy/diagnoses.json", found["fix"])

    def test_a_failure_whose_output_is_withheld_says_where_it_is_sealed(self):
        # As GitHub prints them: a title is not in the log, only the message.
        cases = {
            "##[error]2 extension test(s) failed; the full output is sealed": "composition-tests",
            "##[error]The composed suite did not run: ImproperlyConfigured; the full": "composition-did-not-run",
            "##[error]The admitted set does not compose. The reason is sealed.": "composition-refused",
            "##[error]A coordinated branch did not build; its output is sealed.": "candidate-build",
            "##[error]The container did not report healthy within 90 seconds.": "image-unhealthy",
        }
        for line, expected in cases.items():
            with self.subTest(expected=expected):
                found = _diagnose().diagnose(f"2026-10-02T00:12:32.36Z {line}")
                self.assertEqual(found["id"], expected)
                self.assertIn("scripts/failure-logs.sh", found["fix"])

    def test_a_message_in_an_echoed_script_is_not_a_failure(self):
        log = (
            "2026-10-02T00:01:00.1Z ##[group]Run set -euo pipefail\n"
            '2026-10-02T00:01:00.2Z echo "::error::COMPOSITION_EXTENSIONS must be set"\n'
            "2026-10-02T00:01:00.3Z env:\n"
            "2026-10-02T00:01:00.4Z ##[endgroup]\n"
            "2026-10-02T00:02:00.0Z FAILED (failures=1)\n"
        )
        self.assertEqual(_diagnose().diagnose(log)["id"], "host-tests")

    def test_a_failing_host_test_is_not_blamed_on_the_composition(self):
        found = _diagnose().diagnose("2026-10-02T00:12:32.36Z FAILED (failures=1, skipped=2)")
        self.assertEqual(found["id"], "host-tests")
        composed = _diagnose().diagnose("2026-10-02T00:12:32.36Z composed suite: FAILED (failures=1)")
        self.assertEqual(composed["id"], "composition-tests")

    def test_it_never_repeats_the_log(self):
        secret = "private-extension-name"
        found = _diagnose().diagnose(f"{secret}: denied: permission_denied: write_package")

        self.assertNotIn(secret, json.dumps(found))

    def test_every_entry_is_complete_and_its_pattern_compiles(self):
        entries = json.loads((ROOT / "deploy" / "diagnoses.json").read_text())["diagnoses"]
        ids = [entry["id"] for entry in entries]

        self.assertEqual(len(ids), len(set(ids)))
        for entry in entries:
            self.assertEqual(set(entry), {"id", "match", "title", "fix"})
            re.compile(entry["match"])
