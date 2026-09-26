"""System checks read whatever is on disk without stopping startup."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from core import checks


class UnreadableFilesTests(SimpleTestCase):
    def test_bad_bytes_and_unreadable_paths_are_skipped_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "page.html").write_bytes(b"\xff\xfe" + checks.EM_DASH.encode())
            (root / "binary.py").write_bytes(b"x = '\xff'\n")
            (root / "nulls.py").write_bytes(b"x = 1\x00\n")
            (root / "folder.html").mkdir()
            (root / "folder.py").mkdir()
            with patch.object(checks, "_roots", return_value=[root]):
                found = checks.em_dashes()
                checks.hand_plurals()
                checks.nested_forms()
        self.assertEqual(found, [(root / "page.html", 1)])


class StaticLiveNeedsDebugTests(SimpleTestCase):
    def test_static_live_without_debug_is_an_error(self):
        with override_settings(STATIC_LIVE=True, DEBUG=False):
            errors = checks.static_live_needs_debug()
        self.assertEqual([error.id for error in errors], ["hq.E110"])

    def test_static_live_with_debug_or_off_passes(self):
        for live, debug in ((True, True), (False, False), (False, True)):
            with self.subTest(live=live, debug=debug), override_settings(
                STATIC_LIVE=live, DEBUG=debug
            ):
                self.assertEqual(checks.static_live_needs_debug(), [])


class StaticLiveIsADeployCheckTests(SimpleTestCase):
    def test_it_runs_only_with_deploy_checks(self):
        from django.core.checks import registry

        self.assertNotIn(
            checks.static_live_needs_debug,
            registry.registry.get_checks(include_deployment_checks=False),
        )
        self.assertIn(
            checks.static_live_needs_debug,
            registry.registry.get_checks(include_deployment_checks=True),
        )
