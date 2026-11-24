"""What the sheets hold together, which Stylelint, reading one sheet at a time, cannot."""

import re

from django.test import SimpleTestCase, tag

from hq.platform.core.stylesheets import sheet_paths
from hq.platform.core.table_layout import TRACKS

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
NOWRAP = re.compile(r"white-space\s*:\s*nowrap")
WRAPS = re.compile(r"white-space\s*:\s*(?:normal|pre-wrap|pre-line|break-spaces)|text-wrap-mode\s*:\s*wrap")
# A selector whose subject is a control: a `.btn` or a `button`, last in it.
BUTTON = re.compile(r"(?:\.btn\b|(?<![\w.#-])button\b)[^\s>+~,]*\)?\s*(?:,|$)")
CLIPPED = re.compile(r"overflow(?:-x)?\s*:\s*(?:hidden|clip)")
# A table's tracks after its row's tick-box: the one that names the rows, then the rest.
TABLE_TRACKS = re.compile(r"grid-template-columns:\s*var\(--lead,\)\s*auto\s+repeat\((\d+),")
# The push button's rule (page.css).
PUSH_BUTTON = ".btn, :where(button:not([class]))"


def _uncommented(css: str) -> str:
    return re.sub(r"/\*.*?\*/", " ", css, flags=re.DOTALL)


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


@tag("source", "tree")
class SheetTests(SimpleTestCase):
    def test_every_size_query_names_a_container_the_stylesheets_declare(self):
        """An unnamed size query is asked of whichever size container happens
        to be nearest, and of none once that one stops being a container, so it
        silently stops matching. A named one is asked of its own container, and
        a name nothing declares is the same silence."""

        css = "\n".join(_uncommented(path.read_text(encoding="utf-8")) for path in sheet_paths())
        unnamed, undeclared = container_query_faults(css)

        self.assertEqual(unnamed, [], "a size query names the container it is asked of")
        self.assertEqual(undeclared, [], "every container a query names is declared")

    def test_a_tables_grid_has_a_track_for_each_column_it_may_draw(self):
        """A row's grid is its table's, so a column past the last track has
        none of its own and is drawn over that one. `DataTable` refuses more
        columns than ``TRACKS``, and the grid holds that many."""

        sheet = next(path for path in sheet_paths() if path.name == "tables.css")

        self.assertEqual(TABLE_TRACKS.findall(sheet.read_text(encoding="utf-8")), [str(TRACKS - 1)])

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
        held = [name for name, selector, body in rules if selector == PUSH_BUTTON and NOWRAP.search(body)]
        restated = [
            f"{name}: {selector}"
            for name, selector, body in rules
            if selector != PUSH_BUTTON and BUTTON.search(selector) and NOWRAP.search(body) and not CLIPPED.search(body)
        ]

        # A flat line beneath an opened row on a phone stands its buttons one
        # to a line across it, so a label longer than the phone has nothing
        # left to give way and wraps.
        spans_the_line = 'tables.css: .data-table[data-line="flat"] .row-columns > dd .btn'
        wrapped = [
            f"{name}: {selector}"
            for name, selector, body in rules
            if BUTTON.search(selector) and WRAPS.search(body) and f"{name}: {selector}" != spans_the_line
        ]

        self.assertEqual(held, ["page.css"], "the button rule keeps a control's label on one line")
        self.assertEqual(restated, [], "a control's line is held by the button rule alone")
        self.assertEqual(wrapped, [], "a control's label keeps its line; its column gives way")
