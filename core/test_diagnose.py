"""The pipeline's plain-English diagnoses (scripts/diagnose.py)."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[1]


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

    def test_nothing_known_says_so_and_asks_for_an_entry(self):
        found = _diagnose().diagnose("something nobody has seen")

        self.assertEqual(found["id"], "unknown")
        self.assertIn("deploy/diagnoses.json", found["fix"])

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
