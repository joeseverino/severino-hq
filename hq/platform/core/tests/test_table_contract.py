"""A data table is declared (``tables.DataTable``), so its shape is in the HTML on first paint."""

import re
from pathlib import Path

from django.template import Context, Template
from django.test import SimpleTestCase, tag

from hq.platform.core.part_examples import table
from hq.platform.core.stylesheets import stylesheet as whole_stylesheet

TEMPLATES = Path(__file__).resolve().parents[4] / "templates"


@tag("source", "tree")
class TableContractTests(SimpleTestCase):
    def test_the_gutter_reads_nothing_a_script_marks(self):
        """The gutter moves every name in its column, so whether it is kept
        follows from the server's markup and the box's width, never from what
        tables.js marks after paint."""

        root = TEMPLATES.parent
        script = (root / "static" / "js" / "tables.js").read_text(encoding="utf-8")
        marked = {
            re.sub(r"[A-Z]", lambda upper: "-" + upper.group().lower(), key)
            for key in re.findall(r"dataset\.(\w+)\s*=", script)
        }
        marked |= set(re.findall(r'toggleAttribute\("data-([\w-]+)"', script))
        self.assertIn("row-opened", marked)
        stylesheet = re.sub(r"/\*.*?\*/", " ", whole_stylesheet(), flags=re.DOTALL)
        gutters = [
            selector
            for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", stylesheet)
            if "var(--row-gutter)" in block
        ]
        self.assertTrue(gutters)
        read = {(name, selector.strip()) for selector in gutters for name in marked if f"data-{name}" in selector}
        self.assertEqual(read, set())


class TableTagTests(SimpleTestCase):
    def test_what_would_fill_a_table_is_asked_only_when_it_is_empty(self):
        """A remedy may read the database (`page.remedy`), so a table with
        rows, which never shows one, never asks for it."""

        asked = []

        def remedy():
            asked.append(True)
            return {"label": "Add a run", "url": "/runs/new/"}

        for rows in (("a",), ()):
            runs = table("Runs", ("Name",), *rows, region={"remedy": remedy})
            Template("{% data_table runs %}").render(Context({"runs": runs}))
        self.assertEqual(asked, [True])
