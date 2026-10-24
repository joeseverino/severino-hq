"""The stylesheet is its sheets, all of them, linked in one cascade order."""

import re

from django.templatetags.static import static
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from hq.platform.application.tests.markup import elements
from hq.platform.core.stylesheets import DIRECTORY, SHEETS, STATIC, sheet_paths, stylesheet

LAYER_ORDER = re.compile(r"@layer\s+([\w\s,.-]+);")
LAYER_BLOCK = re.compile(r"@layer\s+([\w.-]+)\s*\{")
CONTAINER_QUERY = re.compile(r"@container\s+([^{]+)\{")
QUERY_NAME = re.compile(r"([a-zA-Z_][\w-]*)\s*(?=\(|not\b|style\()")
# A query of a custom property's value, asked or negated: a style query is
# asked of the element's parent, never of a size container.
STYLE_QUERY = re.compile(r"(?:not\s+)?style\(")
CONTAINER_NAMES = re.compile(r"(?:^|[{;])\s*container(?:-name)?\s*:\s*([^;{}/]+)")
# A rule with no rule nested in it: its selector and its declarations.
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
CELL = re.compile(r"(?<![\w.#-])(?:td|th)(?![\w-])")
HELD_WIDTH = re.compile(r"(?:^|;)\s*min-width\s*:(?!\s*0(?:px)?\s*(?:;|$))", re.MULTILINE)
NOWRAP = re.compile(r"white-space\s*:\s*nowrap")
CLIPPED = re.compile(r"overflow(?:-x)?\s*:\s*(?:hidden|clip)")
# The one-pixel clip that keeps text for a screen reader alone.
CLIP_RECIPE = re.compile(r"clip-path\s*:\s*inset\(50%\)")
TABLE_PRIMITIVES = frozenset({"tables.css", "figures.css"})
FONT_FACE = re.compile(r"@font-face\s*\{[^{}]*\}")
FETCHED = re.compile(r"\burl\(|@import\b")


def _uncommented(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


@tag("source")
class SheetTests(SimpleTestCase):
    def test_every_sheet_in_the_directory_is_linked(self):
        """A sheet left out of the list is never linked, and every check of the
        stylesheet reads past it."""

        written = {path.relative_to(STATIC).as_posix() for path in (STATIC / DIRECTORY).glob("*.css")}

        self.assertEqual(written, set(SHEETS))
        self.assertEqual(len(SHEETS), len(set(SHEETS)))

    def test_the_layer_order_is_declared_once_before_any_layer_opens(self):
        """A layer is ordered where it is first named. One a sheet opens
        without its declaration would be ordered after `components`, and its
        rules would beat every component's."""

        sheets = [_uncommented(path.read_text(encoding="utf-8")) for path in sheet_paths()]
        declared = [order for sheet in sheets for order in LAYER_ORDER.findall(sheet)]
        self.assertEqual(len(declared), 1)
        first = sheets[0]
        self.assertLess(first.index(declared[0]), LAYER_BLOCK.search(first).start())
        layers = {name.strip() for name in declared[0].split(",")}
        opened = {name for sheet in sheets for name in LAYER_BLOCK.findall(sheet)}
        self.assertLessEqual(opened, layers)

    def test_every_size_query_names_a_container_the_stylesheets_declare(self):
        """An unnamed size query is asked of whichever size container happens
        to be nearest, and of none once that one stops being a container, so it
        silently stops matching. A named one is asked of its own container, and
        a name nothing declares is the same silence."""

        css = "\n".join(_uncommented(path.read_text(encoding="utf-8")) for path in sheet_paths())
        declared = {name for names in CONTAINER_NAMES.findall(css) for name in names.split()}
        unnamed, undeclared = [], []
        for prelude in (query.strip() for query in CONTAINER_QUERY.findall(css)):
            if STYLE_QUERY.match(prelude):
                continue
            named = QUERY_NAME.match(prelude)
            if named is None or named.group(1) == "not":
                unnamed.append(prelude)
            elif named.group(1) not in declared:
                undeclared.append(prelude)

        self.assertEqual(unnamed, [], "a size query names the container it is asked of")
        self.assertEqual(undeclared, [], "every container a query names is declared")

    def test_only_the_table_primitives_hold_a_cells_line(self):
        """How wide a cell's line holds out is bounded by its column's share of
        the region (`--hold`, tables.css), so the holds of a table never sum
        past its box. A page's own nowrap or min-width on cell content holds
        out unbounded and widens the table past it. A nowrap that clips is a
        cut, bounded by its box, and stays allowed."""

        found = []
        for path in sheet_paths():
            if path.name in TABLE_PRIMITIVES:
                continue
            for selector, body in RULE.findall(_uncommented(path.read_text(encoding="utf-8"))):
                if not CELL.search(selector):
                    continue
                unbounded = HELD_WIDTH.search(body) or (NOWRAP.search(body) and not CLIPPED.search(body))
                if unbounded:
                    found.append(f"{path.name}: {' '.join(selector.split())}")

        self.assertEqual(found, [], "a cell's line is held only by the table primitives")

    def test_text_for_a_screen_reader_alone_is_hidden_by_the_one_class(self):
        """What only a screen reader hears is `.visually-hidden`
        (cadence.css), never the same recipe restated under another selector,
        so a change to how it is clipped is made once."""

        found = [
            " ".join(selector.split())
            for selector, body in RULE.findall(_uncommented(stylesheet()))
            if CLIP_RECIPE.search(body) and selector.strip() != ".visually-hidden"
        ]
        self.assertEqual(found, [])

    def test_a_sheet_fetches_nothing_but_its_faces(self):
        """An image or sheet a rule fetches arrives after the page is drawn and
        moves what it lands beside, past the moment the browser gate measures
        (`_DRAWN`). A face is the one fetch `document.fonts` reports, so the
        gate waits for it."""

        found = []
        for path in sorted((STATIC / "css").rglob("*.css")):
            css = FONT_FACE.sub(" ", _uncommented(path.read_text(encoding="utf-8")))
            found += [f"{path.relative_to(STATIC)}: {match.group()}" for match in FETCHED.finditer(css)]

        self.assertEqual(found, [], "a sheet fetches only a face, in @font-face")


class LinkedSheetTests(TestCase):
    def test_a_page_links_every_sheet_in_cascade_order(self):
        response = self.client.get(reverse("login"))

        links = [link.attrs["href"] for link in elements(response, "link") if link.attrs.get("rel") == "stylesheet"]
        self.assertEqual(links, [static(sheet) for sheet in SHEETS])
