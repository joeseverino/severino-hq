"""The pipeline's plain-English diagnoses (scripts/diagnose.py)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).parent.resolve().parents[1]


def _diagnose():
    spec = importlib.util.spec_from_file_location("diagnose", ROOT / "scripts" / "diagnose.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DiagnoseTests(SimpleTestCase):
    def test_a_known_failure_is_named_with_its_fix(self):
        found = _diagnose().diagnose("#15 pushing\\ndenied: permission_denied: write_package\\n##[error]Process completed")

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
        cases = {
            "::error title=Composed suite::2 extension test(s) failed": "composition-tests",
            "::error title=Composed suite did not run::ImproperlyConfigured": "composition-did-not-run",
            "::error title=Composition refused::The admitted set does not compose.": "composition-refused",
            "::error title=Coordinated branch did not build::": "candidate-build",
            "::error title=Image not healthy::": "image-unhealthy",
        }
        for log, expected in cases.items():
            with self.subTest(expected=expected):
                found = _diagnose().diagnose(log)
                self.assertEqual(found["id"], expected)
                self.assertIn("scripts/failure-logs.sh", found["fix"])

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
