"""System checks read whatever is on disk without stopping startup."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from hq.platform.core import checks


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


class CountedPhraseCheckTests(SimpleTestCase):
    """A literal phrase that cannot agree fails the check, not a page render."""

    def found(self, files, plugin_apps=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for name, text in files.items():
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)
            with (
                patch.object(checks, "_roots", return_value=[root / "host"]),
                patch("hq.platform.application.plugins.installed_plugin_apps", return_value=list(plugin_apps)),
                patch.object(sys, "path", [str(root), *sys.path]),
            ):
                return [
                    (path.relative_to(root).as_posix(), line)
                    for path, line, _ in checks.unagreeable_counts()
                ]

    def test_a_template_phrase_without_its_plural_is_found(self):
        found = self.found({
            "host/page.html": (
                '{{ n|counted:"zone band" }}\n'
                '{{ n|counted:"zone band,zone bands" }}\n'
                "{{ n|counted:'change' }}\n"
                "{{ n|counted:'' }}\n"
            ),
        })
        self.assertEqual(found, [("host/page.html", 1), ("host/page.html", 4)])

    def test_a_python_call_with_literal_words_is_found(self):
        found = self.found({
            "host/views.py": (
                "counted(n, 'content item')\n"
                "counted(n, 'content item', 'content items')\n"
                "ui.counted(n, 'row needs you', many='rows need you')\n"
                "counted(n, 'change')\n"
                "counted(n, phrase)\n"
                "counted(n, 'zone band', plural)\n"
                "ui.counted(n, 'zone band')\n"
            ),
        })
        self.assertEqual(found, [("host/views.py", 1), ("host/views.py", 7)])

    def test_an_installed_extension_is_read_wherever_it_is_installed(self):
        found = self.found(
            {
                "example_installed/__init__.py": "",
                "example_installed/templates/example_installed/list.html": (
                    '{{ n|counted:"record needs review" }}\n'
                ),
            },
            plugin_apps=["example_installed"],
        )
        self.assertEqual(found, [("example_installed/templates/example_installed/list.html", 1)])

    def test_it_is_an_error(self):
        with patch.object(
            checks, "unagreeable_counts", return_value=[(Path("page.html"), 3, "why")]
        ):
            errors = checks.counted_phrases_agree()
        self.assertEqual([(error.id, error.level) for error in errors], [("hq.E101", 40)])

    def test_the_rule_is_the_one_counted_applies(self):
        from hq.platform.application.ui import counted

        for phrase in ("zone band", ""):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                counted(2, phrase)
