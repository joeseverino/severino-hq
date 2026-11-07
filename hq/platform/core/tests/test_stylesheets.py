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
# A query of whether a sticky container is stuck or scrolled, which is asked
# of a scroll-state container, never of a size one.
SCROLL_STATE_QUERY = re.compile(r"([a-zA-Z_][\w-]*)\s+scroll-state\(")
SCROLL_STATE = re.compile(r"\bscroll-state\b")
# A rule with no rule nested in it: its selector and its declarations.
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
CELL = re.compile(r"(?<![\w.#-])(?:td|th)(?![\w-])")
NOWRAP = re.compile(r"white-space\s*:\s*nowrap")
WRAPS = re.compile(r"white-space\s*:\s*(?:normal|pre-wrap|pre-line|break-spaces)|text-wrap-mode\s*:\s*wrap")
# A selector whose subject is a control: a `.btn` or a `button`, last in it.
BUTTON = re.compile(r"(?:\.btn\b|(?<![\w.#-])button\b)[^\s>+~,]*\)?\s*(?:,|$)")
CLIPPED = re.compile(r"overflow(?:-x)?\s*:\s*(?:hidden|clip)")
# The one-pixel clip that keeps text for a screen reader alone.
CLIP_RECIPE = re.compile(r"clip-path\s*:\s*inset\(50%\)")
# The sheets the table primitive is drawn in (docs/Design.md).
TABLE_PRIMITIVES = frozenset({"base.css", "cuts.css", "figures.css", "tables.css", "surfaces.css"})
FONT_FACE = re.compile(r"@font-face\s*\{[^{}]*\}")
FETCHED = re.compile(r"\burl\(|@import\b")


def _uncommented(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


@tag("source", "tree")
def _scroll_state_containers(css: str) -> set[str]:
    """The names a rule declares with a scroll-state container type."""

    names = set()
    for _selector, body in RULE.findall(css):
        if not SCROLL_STATE.search(body):
            continue
        names |= {name for found in CONTAINER_NAMES.findall(body) for name in found.split()}
    return names


def container_query_faults(css: str) -> tuple[list[str], list[str]]:
    """The container queries in ``css`` that name no container, and those
    that name one no sheet declares: a size query a declared container, a
    scroll-state query one declared with `container-type: scroll-state`."""

    declared = {name for names in CONTAINER_NAMES.findall(css) for name in names.split()}
    scrolled = _scroll_state_containers(css)
    unnamed, undeclared = [], []
    for prelude in (query.strip() for query in CONTAINER_QUERY.findall(css)):
        if STYLE_QUERY.match(prelude):
            continue
        if SCROLL_STATE.search(prelude):
            named = SCROLL_STATE_QUERY.match(prelude)
            if named is None:
                unnamed.append(prelude)
            elif named.group(1) not in scrolled:
                undeclared.append(prelude)
            continue
        named = QUERY_NAME.match(prelude)
        if named is None or named.group(1) == "not":
            unnamed.append(prelude)
        elif named.group(1) not in declared:
            undeclared.append(prelude)
    return unnamed, undeclared


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
        unnamed, undeclared = container_query_faults(css)

        self.assertEqual(unnamed, [], "a size query names the container it is asked of")
        self.assertEqual(undeclared, [], "every container a query names is declared")

    def test_a_scroll_state_query_is_held_to_a_scroll_state_container(self):
        declared = ".bar { container: bar / scroll-state; } .size { container-name: size; }"

        self.assertEqual(
            container_query_faults(declared + "@container bar scroll-state(stuck: bottom) { .a { color: red; } }"),
            ([], []),
        )
        self.assertEqual(
            container_query_faults(declared + "@container size scroll-state(stuck: bottom) { .a { color: red; } }"),
            ([], ["size scroll-state(stuck: bottom)"]),
        )
        self.assertEqual(
            container_query_faults(declared + "@container scroll-state(stuck: bottom) { .a { color: red; } }"),
            (["scroll-state(stuck: bottom)"], []),
        )

    def test_only_the_table_primitives_hold_a_cells_line(self):
        """How wide a cell's line holds out is bounded by its column's share of
        the region (`--hold`, tables.css), so the holds of a table never sum
        past its box. A page's own nowrap on cell content holds out unbounded
        and widens the table past it; a page's width there is refused by
        `lint:css`. A nowrap that clips is a cut, bounded by its box, and
        stays allowed."""

        found = []
        for path in sheet_paths():
            if path.name in TABLE_PRIMITIVES:
                continue
            for selector, body in RULE.findall(_uncommented(path.read_text(encoding="utf-8"))):
                if not CELL.search(selector):
                    continue
                unbounded = NOWRAP.search(body) and not CLIPPED.search(body)
                if unbounded:
                    found.append(f"{path.name}: {' '.join(selector.split())}")

        self.assertEqual(found, [], "a cell's line is held only by the table primitives")

    def test_a_controls_label_keeps_its_line_from_the_button_rule_alone(self):
        """A label wrapped onto two or three lines reads as several controls
        and leaves the buttons beside it uneven. The button rule holds every
        label to one line, so no page or column restates it, and no rule
        lets one wrap while a column can give way to its labels. A control
        whose label is cut (cuts.css) holds its line for the cut."""

        rules = [
            (path.name, " ".join(selector.split()), body)
            for path in sheet_paths()
            for selector, body in RULE.findall(_uncommented(path.read_text(encoding="utf-8")))
        ]
        held = [name for name, selector, body in rules if selector == ".btn, button" and NOWRAP.search(body)]
        restated = [
            f"{name}: {selector}"
            for name, selector, body in rules
            if selector != ".btn, button"
            and BUTTON.search(selector)
            and NOWRAP.search(body)
            and not CLIPPED.search(body)
        ]

        # An opened queue row on a phone stands its buttons one to a line
        # across the row, so a label longer than the phone has nothing left
        # to give way and wraps.
        spans_the_line = "cards.css: .queue-table .row-detail .row-columns > dd .btn"
        wrapped = [
            f"{name}: {selector}"
            for name, selector, body in rules
            if BUTTON.search(selector) and WRAPS.search(body) and f"{name}: {selector}" != spans_the_line
        ]

        self.assertEqual(held, ["page.css"], "the button rule keeps a control's label on one line")
        self.assertEqual(restated, [], "a control's line is held by the button rule alone")
        self.assertEqual(wrapped, [], "a control's label keeps its line; its column gives way")

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
