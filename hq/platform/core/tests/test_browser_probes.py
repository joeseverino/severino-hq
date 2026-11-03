"""What the browser gate hands the page, held without a browser."""

import re

from django.test import SimpleTestCase

from hq.platform.core.stylesheets import SHEETS
from hq.platform.core.tests.browser import (
    accessibility,
    catalog,
    command_forms,
    components,
    current_state,
    estate,
    folds,
    fragments,
    gate,
    in_flight,
    layout,
    page_errors,
    planted,
    reading,
    reference,
    selectors,
    serving,
    shots,
    stress,
    survey,
    sweeps,
    uncaught,
    widths,
)
from hq.platform.core.tests.browser.probes import (
    axe,
    boxes,
    describe,
    palette,
    parts,
    settle,
    shifts,
    tables,
    text,
)
from hq.platform.core.tests.browser.probes.palette import LINE_CONTRAST, losses, lost_fills
from hq.platform.core.tests.browser.serving import static_file
from hq.platform.core.tests.browser.survey import AUDIT, survey_checks
from hq.platform.core.tests.browser.widths import WIDE
from hq.platform.core.tests.browser_modules import browser_modules

# Imported by name rather than found, so a change to any of them selects these
# tests in the fast gate; test_every_browser_module_is_read holds the list whole.
MODULES = (
    accessibility,
    catalog,
    command_forms,
    components,
    current_state,
    estate,
    folds,
    fragments,
    gate,
    in_flight,
    layout,
    page_errors,
    planted,
    reading,
    reference,
    selectors,
    serving,
    shots,
    stress,
    survey,
    sweeps,
    uncaught,
    widths,
    axe,
    boxes,
    describe,
    palette,
    parts,
    settle,
    shifts,
    tables,
    text,
)

_WALK = re.compile(r"\bcreate(?:TreeWalker|NodeIterator)\(")


def _arguments(source: str, start: int) -> list[str]:
    """The top-level arguments of the call whose ``(`` is at ``start``."""

    depth, current, found = 0, [], []
    for char in source[start:]:
        if char in "([{":
            depth += 1
            if depth == 1:
                continue
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return [*found, "".join(current)]
        elif char == "," and depth == 1:
            found.append("".join(current))
            current = []
            continue
        current.append(char)
    raise AssertionError(f"unbalanced call in {source[start : start + 60]!r}")


def walks_with_a_callback(source: str) -> list[str]:
    """Each tree walk in ``source`` that hands the engine a filter to call."""

    return [
        source[match.start() : match.start() + 80]
        for match in _WALK.finditer(source)
        if len(_arguments(source, match.end() - 1)) > 2
    ]


def probes() -> dict[str, str]:
    """Every probe source the gate holds, and the audit's."""

    held = {
        f"{module.__name__}.{name}": value
        for module in MODULES
        for name, value in vars(module).items()
        if name.startswith("_") and isinstance(value, str)
    }
    return held | {AUDIT.name: AUDIT.read_text(encoding="utf-8")}


class ProbeTests(SimpleTestCase):
    def test_every_browser_module_is_read(self):
        self.assertEqual(
            sorted(module.__name__ for module in MODULES),
            sorted(browser_modules()),
            "a browser module is imported and listed in MODULES above",
        )

    def test_no_probe_walks_the_page_with_a_callback(self):
        """An engine with script off refuses to call a page function back, so
        a walk with a filter throws there instead of reading the page."""

        self.assertTrue(walks_with_a_callback("document.createTreeWalker(el, NodeFilter.SHOW_TEXT, (n) => f(n, 1))"))
        self.assertFalse(walks_with_a_callback("document.createTreeWalker(f(el, 1), NodeFilter.SHOW_TEXT)"))
        self.assertTrue(any(_WALK.search(source) for source in probes().values()), "the walk found no probe")
        self.assertEqual(
            {name: walks for name, source in probes().items() if (walks := walks_with_a_callback(source))}, {}
        )


class PlantTests(SimpleTestCase):
    def test_every_geometry_check_has_a_known_bad_case(self):
        """A check is proven able to fail by the plant PlantedBrowserTests
        requires it to find: one with none could pass every page forever."""

        checks = survey_checks("/", WIDE)
        self.assertIn("sideways", checks)
        self.assertEqual(sorted(set(checks) - set(planted.PLANTS)), [], "a check with no plant in planted.PLANTS")
        self.assertEqual(sorted(set(planted.PLANTS) - set(checks)), [], "a plant for no check the survey runs")


class LostFillTests(SimpleTestCase):
    """The palettes are one set of distinctions: a box one sets apart, the
    other does too."""

    def test_a_fill_one_palette_loses_is_named_with_the_palette_that_loses_it(self):
        steps = {
            "light": {"4": [1.15, "code (light)"], "9": [1.02, "tag (light)"]},
            "dark": {"4": [1.04, "code (dark)"], "9": [1.2, "tag (dark)"]},
        }
        found = lost_fills(steps, LINE_CONTRAST)
        self.assertEqual(len(found), 2)
        self.assertIn("dark loses code (dark)", found[0])
        self.assertIn("light loses tag (light)", found[1])

    def test_a_fill_both_show_or_both_keep_faint_passes(self):
        steps = {
            "light": {"1": [1.3, "chip"], "2": [1.04, "raised row"]},
            "dark": {"1": [1.2, "chip"], "2": [1.05, "raised row"]},
        }
        self.assertEqual(lost_fills(steps, LINE_CONTRAST), [])

    def test_a_fill_painted_in_one_palette_only_is_lost_in_the_other(self):
        steps = {"light": {"3": [1.2, "active item"]}, "dark": {}}
        self.assertEqual(len(lost_fills(steps, LINE_CONTRAST)), 1)
        self.assertIn("dark loses active item", lost_fills(steps, LINE_CONTRAST)[0])

    def test_a_lost_fill_says_what_paints_it_in_the_palette_that_loses_it(self):
        steps = {
            "light": {"4": [1.15, "code (light)"], "9": [1.02, "tag (light)"]},
            "dark": {"4": [1.04, "code (dark)"], "9": [1.2, "tag (dark)"]},
        }
        self.assertEqual(losses(steps, LINE_CONTRAST), [("4", "dark"), ("9", "light")])
        found = lost_fills(steps, LINE_CONTRAST, {"4": "--code-bg in dark", "9": "no token paints this"})
        self.assertTrue(found[0].startswith("dark loses code (dark)"), found[0])
        self.assertTrue(found[0].endswith("--code-bg in dark"), found[0])
        self.assertTrue(found[1].endswith("no token paints this"), found[1])


class StaticFileTests(SimpleTestCase):
    def test_every_face_the_gate_serves_waits_for_its_font(self):
        """A face the browser may skip when late would be read in either face
        from run to run, so the gate's copy of the stylesheet waits for it."""

        served = b"\n".join(static_file(sheet)[1] for sheet in SHEETS)
        self.assertIn(b"@font-face", served)
        self.assertEqual(re.findall(rb"font-display:\s*optional", served), [])
