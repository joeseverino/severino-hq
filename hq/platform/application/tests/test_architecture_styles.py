"""The style bundle as the contract extensions render against."""

import re
from unittest import mock

from django.test import SimpleTestCase, tag

from hq.platform.core.stylesheets import STATIC, sheet_paths, stylesheet
from hq.platform.core.template_contract import class_words

from .hand_rolled import hand_rolled_classes, needless_classes
from .source import template_files

_TABLE_PARTS = frozenset({"table", "thead", "tbody", "tfoot", "tr", "td", "th"})
_BOXES = frozenset({"block", "flex", "grid", "inline-flex", "inline-grid"})


def _table_part_classes() -> set[str]:
    """Every class the templates put on a table part."""

    cell_classes: set[str] = set()
    for _name, template in template_files():
        for attrs in re.findall(
            r"<(?:table|thead|tbody|tfoot|tr|td|th)\b([^>]*)>",
            template.read_text(encoding="utf-8"),
        ):
            found = re.search(r'class="([^"{}]*)"', attrs)
            if found:
                cell_classes.update(found.group(1).split())
    return cell_classes


def _styles_a_table_part(selector: str, display: str, cell_classes: set[str]) -> bool:
    """Whether one selector of a rule declaring ``display`` styles a table part itself."""

    # The subject is the rightmost compound selector: what the rule
    # actually styles, rather than what it is scoped by.
    subject = selector.rsplit(maxsplit=1)[-1].rsplit(">", maxsplit=1)[-1].strip()
    # A part's pseudo-element is a box inside the part, not the part:
    # giving it a display leaves the cell in table layout.
    if "::" in subject:
        return False
    # An element qualifier settles it either way: `span.x` cannot
    # match a table part however `.x` is used elsewhere, and `td.x`
    # always does. Only an unqualified class has to be judged by
    # where the templates put it.
    qualifier = re.match(r"^([A-Za-z][\w-]*)", subject)
    if qualifier:
        return qualifier.group(1) in _TABLE_PARTS
    # `block` is common on spans that share a class with a cell
    # (`.muted`), so it is judged only where a table part is named.
    if display == "block":
        return False
    return bool(set(re.findall(r"\.([A-Za-z0-9_-]+)", subject)) & cell_classes)


@tag("source", "tree")
class StyleContractTests(SimpleTestCase):
    """The style bundle is a contract that extensions render against.

    These guard failures that are invisible in review and silent at runtime:
    CSS resolves an undefined custom property to an invalid value rather than
    erroring, so a typo'd token degrades to a browser default (a black SVG
    fill) instead of breaking loudly.
    """

    @staticmethod
    def _stylesheet() -> str:
        return stylesheet()

    def test_a_table_part_is_never_given_a_block_flex_or_grid_display(self):
        """A table is laid out by its primitive alone (tables.css, a grid whose
        rows are its subgrids), so a box display any other sheet gives a table
        part takes the row out of its tracks: its cells no longer line up with
        the columns, and neither fails loudly. This reads the classes the
        templates actually put on table parts and rejects the declaration
        there. Lay out a div inside the cell instead.
        """

        cell_classes = _table_part_classes()
        offenses = []
        # Comments first: one sitting above a rule is otherwise read as part of
        # that rule's selector.
        sheets = "\n".join(path.read_text(encoding="utf-8") for path in sheet_paths() if path.name != "tables.css")
        source = re.sub(r"/\*.*?\*/", " ", sheets, flags=re.DOTALL)
        for selector, block in re.findall(r"([^{}]+)\{([^{}]*)\}", source):
            declared = re.search(r"(?<![\w-])display\s*:\s*([a-z-]+)", block)
            if not declared or declared.group(1) not in _BOXES:
                continue
            offenses.extend(
                f"{part.strip()} sets display: {declared.group(1)}"
                for part in selector.split(",")
                if part.strip() and _styles_a_table_part(part.strip(), declared.group(1), cell_classes)
            )

        self.assertEqual(sorted(set(offenses)), [])

    # Viewport breakpoints left, each for something the viewport genuinely
    # decides: the header and nav, a modal that takes the whole screen, table
    # sizing, the head's overflow menu, the dashboard strip, a few dense
    # diagrams. It only goes down. A layout answers to its own width with a
    # fluid rule (auto-fit, flex-wrap, clamp) or a container query instead.
    VIEWPORT_BREAKPOINTS = 10

    def test_viewport_breakpoints_only_go_down(self):

        css = re.sub(r"/\*.*?\*/", " ", self._stylesheet(), flags=re.DOTALL)
        found = [q for q in re.findall(r"@media[^{]*\{", css) if "width" in q]
        self.assertLessEqual(
            len(found),
            self.VIEWPORT_BREAKPOINTS,
            "A new viewport breakpoint: make the layout fluid or ask its container "
            "(docs/Design.md, 'Size to content, fit the window').",
        )

    def test_a_selector_is_styled_in_one_place(self):
        """A component's rule lives once, so a change to it is made once.

        Only rules naming a single selector are counted: a shared list (the
        frame rule, the band rule) and a state beside its base (`a, a:hover`)
        are the idioms that put one name in two rules on purpose. Breakpoint
        and container variants are the same rule at another size and are not
        counted either.

        Stylelint's no-duplicate-selectors does not replace it: it judges one
        file and one at-rule block at a time, so a selector in two sheets or
        in two `@layer` blocks of one sheet passes it.
        """

        import collections

        css = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group().count("\n"), self._stylesheet(), flags=re.DOTALL)
        seen: dict[str, list[int]] = collections.defaultdict(list)
        stack: list[str] = []
        start = 0
        for match in re.finditer(r"[{}]", css):
            if match.group() == "{":
                selector = " ".join(css[start : match.start()].split())
                stack.append(selector)
                outer = stack[:-1]
                top_level = all(s.startswith("@layer") for s in outer)
                single = "," not in re.sub(r"\([^()]*\)", "", selector)
                if top_level and single and not selector.startswith("@"):
                    seen[selector].append(css.count("\n", 0, match.start()) + 1)
            elif stack:
                stack.pop()
            start = match.end()
        twice = {selector: lines for selector, lines in seen.items() if len(lines) > 1}
        self.assertEqual(twice, {})

    def test_a_fallback_never_hides_an_undefined_custom_property(self):
        """A var() with a fallback still names a property some sheet defines.

        Stylelint's no-unknown-custom-properties (`lint:css`) passes any
        var() that carries a fallback, and a typo there degrades silently to
        the fallback.
        """

        css = "\n".join(path.read_text(encoding="utf-8") for path in sorted((STATIC / "css").rglob("*.css")))
        defined = set(re.findall(r"(--[\w-]+)\s*:", css))
        fallen_back = set(re.findall(r"var\(\s*(--[\w-]+)\s*,", css))
        self.assertTrue(fallen_back)
        self.assertEqual(sorted(fallen_back - defined), [])

    def test_categorical_series_slots_are_defined_and_distinct(self):

        css = self._stylesheet()
        slots = re.findall(
            r"^\s*(--series-\d+)\s*:\s*light-dark\((#[0-9a-fA-F]{6}),\s*(#[0-9a-fA-F]{6})\)",
            css,
            re.MULTILINE,
        )
        self.assertGreaterEqual(len(slots), 5, "expected at least 5 categorical slots")
        for theme, index in (("light", 1), ("dark", 2)):
            values = [slot[index].lower() for slot in slots]
            with self.subTest(theme=theme):
                self.assertEqual(len(values), len(set(values)), "series slots must be distinct")


def defined_classes() -> set[str]:
    """Every class a stylesheet HQ serves defines: the bundle every page
    links, and a sheet one page links on its own."""

    sheets = (path.read_text(encoding="utf-8") for path in (STATIC / "css").rglob("*.css"))
    return {name for sheet in sheets for name in re.findall(r"\.([a-z][a-z0-9-]*)", sheet)}


@tag("source", "tree")
class TemplateClassStyleTests(SimpleTestCase):
    """Every class a template names is one the stylesheet defines.

    A class the bundle lacks raises no error; the element is unstyled,
    so a page that means a primitive and misspells it, or keeps a class whose
    rule is gone, looks wrong with nothing to say why. Read over every
    template Django loads, an extension's included, since extensions draw
    with the host's stylesheet.
    """

    maxDiff = None

    def test_template_classes_are_defined_in_the_stylesheet(self):

        defined = defined_classes()
        offenders = sorted(
            f"{name}: .{word}"
            for name, path in template_files()
            for _, word in class_words(path.read_text(encoding="utf-8"))
            if not word.endswith("*") and word not in defined
        )
        self.assertEqual(offenders, [], "define the class where its part is styled, or delete it")

    def test_each_dialog_size_is_one_the_dialog_offers(self):
        """The dialog draws its `size` as a class built as it renders, which the
        literal read above never sees, so a size it does not offer would draw
        a form-width dialog with nothing to say why."""

        defined = defined_classes()
        sizes = [
            (name, size)
            for name, path in template_files()
            for include in re.findall(
                r'{%\s*include\s+"partials/_modal\.html"[^%]*%}', path.read_text(encoding="utf-8")
            )
            for size in re.findall(r'\bsize="([^"]*)"', include)
        ]
        self.assertTrue(sizes, "no dialog names a size, so this check reads nothing")
        self.assertEqual(
            [f"{name}: size={size!r}" for name, size in sizes if f"modal-{size}" not in defined],
            [],
            "name a size the dialog offers (partials/_modal.html), or add it beside .modal-wide in forms.css",
        )


@tag("source", "tree")
class HandRolledClassTests(SimpleTestCase):
    """A class is one part's own, declared on its ``Part``: a template writes
    only the public classes, as an extension does, and a part's partial writes
    besides them its own and those of the parts it draws."""

    def test_host_templates_draw_only_the_classes_they_may(self):
        """A class a template writes that no part lets it is a hand-rolled part
        the survey never sees and an extension cannot draw."""

        self.assertEqual(
            sorted(f"templates/{name}: .{word}" for name, word in hand_rolled_classes()),
            [],
            "a page writes only the public classes (template_contract.public_classes), as an extension does: draw "
            "it through the part that owns it, or grow that part an option; a part's partial writes only the classes "
            "its Part declares, or those of a part it draws",
        )

    def test_a_part_declares_only_the_classes_its_partial_writes(self):
        self.assertEqual(sorted(needless_classes()), [], "remove it from the Part's classes")

    def test_a_class_only_its_marking_module_writes_is_written_and_one_nothing_writes_is_not(self):
        from hq.platform.core import table_layout
        from hq.platform.core.template_parts import Part

        part = Part(
            name="",
            about="A table whose open row its layout marks.",
            partial="partials/_data_table.html",
            marked_in=table_layout,
            classes=("is-expanded", "example-unwritten"),
        )
        with mock.patch(f"{needless_classes.__module__}.parts", return_value=[part]):
            self.assertEqual(needless_classes(), {("partials/_data_table.html", "example-unwritten")})
