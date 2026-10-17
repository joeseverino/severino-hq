"""A stylesheet is served without its comments, and nothing else is taken."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from hq.platform.core.static import without_comments


class WithoutCommentsTests(SimpleTestCase):
    def test_a_comment_goes_with_the_line_it_leaves_blank(self):
        css = "/* What a pill is. */\n.pill { color: red; } /* why red */\n"

        self.assertEqual(without_comments(css), ".pill { color: red; }\n")

    def test_a_comment_inside_a_string_is_the_string(self):
        css = '.a::before { content: "/* not a comment */"; }\n.b { background: url("x/*y*/z.svg"); }\n'

        self.assertEqual(without_comments(css), css)

    def test_a_licence_is_kept(self):
        css = "/*! Licence: keep me */\n.a { color: red; }\n"

        self.assertEqual(without_comments(css), css)

    def test_the_stylesheet_keeps_every_rule(self):
        source = (Path(settings.BASE_DIR) / "static/css/app.css").read_text()
        served = without_comments(source)

        self.assertNotIn("/*", served)
        self.assertEqual(served.count("{"), source.count("{") - _in_comments(source, "{"))
        self.assertLess(len(served), len(source))


def _in_comments(css: str, mark: str) -> int:
    return sum(comment.count(mark) for comment in re.findall(r"/\*.*?\*/", css, re.DOTALL))


class CollectedStylesheetTests(SimpleTestCase):
    """What a collect writes, read back the way the server sends it."""

    def test_what_is_served_and_its_gzip_copy_are_the_files_without_comments(self):
        import gzip
        import json
        import tempfile

        from django.core.management import call_command
        from django.test import override_settings

        with tempfile.TemporaryDirectory() as root:
            storages = {**settings.STORAGES, "staticfiles": {"BACKEND": "hq.platform.core.static.HashedStaticStorage"}}
            with override_settings(STATIC_ROOT=root, STORAGES=storages):
                call_command("collectstatic", interactive=False, clear=True, verbosity=0)
            paths = json.loads((Path(root) / "staticfiles.json").read_text())["paths"]
            collected = {
                name: (
                    (Path(root) / paths[name]).read_bytes(),
                    gzip.decompress((Path(root) / f"{paths[name]}.gz").read_bytes()),
                )
                for name in ("css/app.css", "js/app.js")
            }

        stylesheet, stylesheet_gzipped = collected["css/app.css"]
        self.assertGreater(len(stylesheet), 100_000)
        self.assertNotIn(b"/*", stylesheet)
        self.assertEqual(stylesheet_gzipped, stylesheet)
        script, script_gzipped = collected["js/app.js"]
        source = (Path(settings.BASE_DIR) / "static/js/app.js").read_bytes()
        self.assertGreater(len(script), len(source) // 3)
        self.assertLess(len(script), len(source))
        self.assertEqual(script_gzipped, script)
