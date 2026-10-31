"""The one pass that reads what HQ and its extensions show people."""

import ast
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from hq.platform.core import interface_text

UNAGREEABLE = "x = counted(n, 'zone band')\n"


def read(files, plugin_apps=(), worded=("host",)):
    """Findings over a scratch tree, each path relative to it."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp).resolve()
        for name, text in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(text, bytes):
                path.write_bytes(text)
            else:
                path.write_text(text, encoding="utf-8")
        with (
            patch(
                "hq.platform.application.plugins.installed_plugin_apps",
                return_value=list(plugin_apps),
            ),
            patch.object(sys, "path", [str(root), *sys.path]),
        ):
            reading = interface_text.read([root / name for name in worded])

    def relative(found):
        return [(path.relative_to(root).as_posix(), *rest) for path, *rest in found]

    return {
        "files": reading.files,
        "hand_plurals": relative(reading.hand_plurals),
        "nested_forms": relative(reading.nested_forms),
        "unagreeable_counts": [found[:2] for found in relative(reading.unagreeable_counts)],
    }


class ReadingTests(SimpleTestCase):
    def test_a_plural_built_by_hand_is_found(self):
        found = read(
            {
                "host/page.html": "{{ n }} row{{ n|pluralize }}\n{{ n }} rows\n",
                "host/views.py": ("a = f\"{n} row{'s' if n != 1 else ''}\"\nb = f\"{n} rows\"\n"),
            }
        )
        self.assertEqual(sorted(found["hand_plurals"]), [("host/page.html", 1), ("host/views.py", 1)])

    def test_a_form_opened_inside_a_form_is_found_per_template(self):
        found = read(
            {
                "host/a.html": "<form>\n<form>\n</form>\n",
                "host/b.html": "<form>\n</form>\n<form></form>\n",
            }
        )
        self.assertEqual(found["nested_forms"], [("host/a.html", 2)])

    def test_tests_migrations_and_hidden_trees_are_not_read(self):
        found = read(
            {
                "host/views.py": UNAGREEABLE,
                "host/test_views.py": UNAGREEABLE,
                "host/tests/helpers.py": UNAGREEABLE,
                "host/migrations/0001_initial.py": UNAGREEABLE,
                "host/.venv/lib/module.py": UNAGREEABLE,
                "host/node_modules/package/page.html": "<form><form>",
            }
        )
        self.assertEqual(found["unagreeable_counts"], [("host/views.py", 1)])
        self.assertEqual(found["files"], 1)

    def test_bad_bytes_and_unparsable_source_are_skipped_not_raised(self):
        found = read(
            {
                "host/page.html": b"\xff\xfe<form><form>",
                "host/binary.py": b"x = '\xff'\n",
                "host/nulls.py": b"x = 1\x00\n",
                "host/folder.py/inner.txt": "",
            }
        )
        self.assertEqual(found["nested_forms"], [("host/page.html", 1)])

    def test_each_file_is_parsed_once(self):
        files = {f"host/module_{n}.py": "x = 1\n" for n in range(5)}
        with patch.object(ast, "parse", wraps=ast.parse) as parse:
            found = read(files)
        self.assertEqual(found["files"], 5)
        self.assertEqual(parse.call_count, 5)

    def test_a_root_inside_another_is_read_once(self):
        found = read({"host/inner/views.py": UNAGREEABLE}, worded=("host", "host/inner"))
        self.assertEqual(found["unagreeable_counts"], [("host/inner/views.py", 1)])


class CountedPhraseTests(SimpleTestCase):
    """A literal phrase that cannot agree is found in source, not by a page render."""

    def test_a_template_phrase_without_its_plural_is_found(self):
        found = read(
            {
                "host/page.html": (
                    '{{ n|counted:"zone band" }}\n'
                    '{{ n|counted:"zone band,zone bands" }}\n'
                    "{{ n|counted:'change' }}\n"
                    "{{ n|counted:'' }}\n"
                ),
            }
        )
        self.assertEqual(found["unagreeable_counts"], [("host/page.html", 1), ("host/page.html", 4)])

    def test_a_python_call_with_literal_words_is_found(self):
        found = read(
            {
                "host/views.py": (
                    "counted(n, 'content item')\n"
                    "counted(n, 'content item', 'content items')\n"
                    "ui.counted(n, 'row needs you', many='rows need you')\n"
                    "counted(n, 'change')\n"
                    "counted(n, phrase)\n"
                    "counted(n, 'zone band', plural)\n"
                    "ui.counted(n, 'zone band')\n"
                ),
            }
        )
        self.assertEqual(found["unagreeable_counts"], [("host/views.py", 1), ("host/views.py", 7)])

    def test_an_installed_extension_is_read_wherever_it_is_installed(self):
        found = read(
            {
                "example_installed/__init__.py": "",
                "example_installed/templates/example_installed/list.html": (
                    '{{ n|counted:"record needs review" }} {{ n|pluralize }}\n'
                ),
            },
            plugin_apps=["example_installed"],
        )
        self.assertEqual(
            found["unagreeable_counts"],
            [("example_installed/templates/example_installed/list.html", 1)],
        )
        # Its wording is its own repository's to gate, where it is a checkout.
        self.assertEqual(found["hand_plurals"], [])

    def test_the_rule_is_the_one_counted_applies(self):
        from hq.platform.application.ui import counted

        for phrase in ("zone band", ""):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                counted(2, phrase)
