"""A stylesheet is served without its comments, and nothing else is taken."""

import re
import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, tag

from hq.platform.core.static import HashedStaticStorage, without_comments
from hq.platform.core.stylesheets import SHEETS, sheet_paths


class WithoutCommentsTests(SimpleTestCase):
    def test_a_comment_goes_with_the_line_it_leaves_blank(self):
        css = "/* What a pill is. */\n.pill { color: red; } /* why red */\n"

        self.assertEqual(without_comments(css), ".pill { color: red; }\n")

    def test_a_comment_inside_a_string_is_the_string(self):
        css = '.a::before { content: "/* not a comment */"; }\n.b { background: url("x/*y*/z.svg"); }\n'

        self.assertEqual(without_comments(css), css)

    def test_a_license_is_kept(self):
        css = "/*! License: keep me */\n.a { color: red; }\n"

        self.assertEqual(without_comments(css), css)


@tag("source")
class StylesheetSourceTests(SimpleTestCase):
    """The shipped stylesheet keeps every rule once its comments go."""

    def test_the_stylesheet_keeps_every_rule(self):
        for path in sheet_paths():
            with self.subTest(sheet=path.name):
                source = path.read_text(encoding="utf-8")
                served = without_comments(source)

                self.assertNotIn("/*", served)
                self.assertEqual(served.count("{"), source.count("{") - _in_comments(source, "{"))
                self.assertLess(len(served), len(source))


def _in_comments(css: str, mark: str) -> int:
    return sum(comment.count(mark) for comment in re.findall(r"/\*.*?\*/", css, re.DOTALL))


class SavedAssetTests(SimpleTestCase):
    """What collecting writes for one asset: the whole file, minified, whatever read it before."""

    def saved(self, name, text):
        with tempfile.TemporaryDirectory() as root:
            content = ContentFile(text.encode())
            # Hashing reads the file to its end before it is saved.
            content.read()
            stored = HashedStaticStorage(location=root).save(name, content)
            return (Path(root) / stored).read_text()

    def test_a_stylesheet_is_saved_whole_without_its_comments(self):
        saved = self.saved("css/example.css", ".a { color: red; }\n/* note */\n.b { color: blue; }\n")

        self.assertEqual(saved, ".a { color: red; }\n.b { color: blue; }\n")

    def test_a_script_is_minified_and_keeps_its_license(self):
        source = "/*! License: keep me */\nfunction example(first, second) {\n  return first + second;\n}\n"

        saved = self.saved("js/example.js", source)

        self.assertTrue(saved.startswith("/*! License: keep me */"))
        self.assertIn("function example(", saved)
        self.assertLess(len(saved), len(source))


@tag("source")
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
                for name in (*SHEETS, "js/app.js")
            }

        for sheet, path in zip(SHEETS, sheet_paths(), strict=True):
            with self.subTest(sheet=sheet):
                stylesheet, stylesheet_gzipped = collected[sheet]
                source = path.read_text(encoding="utf-8")
                # Whole: every rule the source writes, outside its comments.
                self.assertEqual(stylesheet.count(b"{"), source.count("{") - _in_comments(source, "{"))
                self.assertNotIn(b"/*", stylesheet)
                self.assertEqual(stylesheet_gzipped, stylesheet)
        script, script_gzipped = collected["js/app.js"]
        source = (Path(settings.BASE_DIR) / "static/js/app.js").read_bytes()
        self.assertGreater(len(script), len(source) // 3)
        self.assertLess(len(script), len(source))
        self.assertEqual(script_gzipped, script)
