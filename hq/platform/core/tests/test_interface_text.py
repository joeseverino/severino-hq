"""The one pass that reads what HQ and its extensions show people."""

from __future__ import annotations

import ast
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from hq.platform.core import interface_text


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
            patch.object(interface_text, "_roots", return_value=[root / name for name in worded]),
            patch(
                "hq.platform.application.plugins.installed_plugin_apps",
                return_value=list(plugin_apps),
            ),
            patch.object(sys, "path", [str(root), *sys.path]),
        ):
            reading = interface_text.read()

    def relative(found):
        return [(path.relative_to(root).as_posix(), *rest) for path, *rest in found]

    return {
        "files": reading.files,
        "em_dashes": relative(reading.em_dashes),
        "hand_plurals": relative(reading.hand_plurals),
        "nested_forms": relative(reading.nested_forms),
        "unagreeable_counts": [found[:2] for found in relative(reading.unagreeable_counts)],
    }


class WordingTests(SimpleTestCase):
    def test_an_em_dash_is_found_outside_comments_and_docstrings(self):
        dash = interface_text.EM_DASH
        found = read({
            "host/page.html": f"{{# {dash} #}}\n<p>one {dash} two</p>\n",
            "host/views.py": f'"""Doc {dash}."""\nLABEL = "one {dash} two"  # {dash}\n',
        })
        self.assertEqual(found["em_dashes"], [("host/page.html", 2), ("host/views.py", 2)])

    def test_a_plural_built_by_hand_is_found(self):
        found = read({
            "host/page.html": "{{ n }} row{{ n|pluralize }}\n{{ n }} account(s)\n",
            "host/views.py": (
                "a = f\"{n} row{'s' if n != 1 else ''}\"\n"
                "b = f\"{n} row(s)\"\n"
                "c = \"Duration(s)\"\n"
            ),
        })
        self.assertEqual(
            sorted(found["hand_plurals"]),
            [("host/page.html", 1), ("host/page.html", 2), ("host/views.py", 1), ("host/views.py", 2)],
        )

    def test_a_form_opened_inside_a_form_is_found_per_template(self):
        found = read({
            "host/a.html": "<form>\n<form>\n</form>\n",
            "host/b.html": "<form>\n</form>\n<form></form>\n",
        })
        self.assertEqual(found["nested_forms"], [("host/a.html", 2)])

    def test_tests_migrations_and_hidden_trees_are_not_read(self):
        dash = interface_text.EM_DASH
        line = f'LABEL = "{dash}"\n'
        found = read({
            "host/views.py": line,
            "host/test_views.py": line,
            "host/tests/helpers.py": line,
            "host/migrations/0001_initial.py": line,
            "host/.venv/lib/module.py": line,
            "host/node_modules/package/page.html": dash,
        })
        self.assertEqual(found["em_dashes"], [("host/views.py", 1)])
        self.assertEqual(found["files"], 1)

    def test_bad_bytes_and_unparsable_source_are_skipped_not_raised(self):
        found = read({
            "host/page.html": b"\xff\xfe" + interface_text.EM_DASH.encode(),
            "host/binary.py": b"x = '\xff'\n",
            "host/nulls.py": b"x = 1\x00\n",
            "host/folder.py/inner.txt": "",
        })
        self.assertEqual(found["em_dashes"], [("host/page.html", 1)])

    def test_each_file_is_parsed_once(self):
        files = {f"host/module_{n}.py": "x = 1\n" for n in range(5)}
        with patch.object(ast, "parse", wraps=ast.parse) as parse:
            found = read(files)
        self.assertEqual(found["files"], 5)
        self.assertEqual(parse.call_count, 5)

    def test_a_root_inside_another_is_read_once(self):
        dash = interface_text.EM_DASH
        found = read(
            {"host/inner/views.py": f'LABEL = "{dash}"\n'}, worded=("host", "host/inner")
        )
        self.assertEqual(found["em_dashes"], [("host/inner/views.py", 1)])


class CountedPhraseTests(SimpleTestCase):
    """A literal phrase that cannot agree is found in source, not by a page render."""

    def test_a_template_phrase_without_its_plural_is_found(self):
        found = read({
            "host/page.html": (
                '{{ n|counted:"zone band" }}\n'
                '{{ n|counted:"zone band,zone bands" }}\n'
                "{{ n|counted:'change' }}\n"
                "{{ n|counted:'' }}\n"
            ),
        })
        self.assertEqual(found["unagreeable_counts"], [("host/page.html", 1), ("host/page.html", 4)])

    def test_a_python_call_with_literal_words_is_found(self):
        found = read({
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
        self.assertEqual(found["unagreeable_counts"], [("host/views.py", 1), ("host/views.py", 7)])

    def test_an_installed_extension_is_read_wherever_it_is_installed(self):
        dash = interface_text.EM_DASH
        found = read(
            {
                "example_installed/__init__.py": "",
                "example_installed/templates/example_installed/list.html": (
                    f'{{{{ n|counted:"record needs review" }}}} {dash}\n'
                ),
            },
            plugin_apps=["example_installed"],
        )
        self.assertEqual(
            found["unagreeable_counts"],
            [("example_installed/templates/example_installed/list.html", 1)],
        )
        # Its wording is its own repository's to gate, where it is a checkout.
        self.assertEqual(found["em_dashes"], [])

    def test_the_rule_is_the_one_counted_applies(self):
        from hq.platform.application.ui import counted

        for phrase in ("zone band", ""):
            with self.subTest(phrase=phrase), self.assertRaises(ValueError):
                counted(2, phrase)
