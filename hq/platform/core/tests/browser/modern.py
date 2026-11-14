"""The gates judge CSS by what the engine running it does, never by how new
it is: a feature newer than an engine, written the way the platform asks, is
one no gate refuses or waits on."""

from unittest import mock

from django.conf import settings

from hq.platform.core.stylesheets import DIRECTORY
from hq.platform.core.tests.browser import components
from hq.platform.core.tests.browser.gate import add_css
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.settle import STILL
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.widths import WIDE
from hq.platform.core.tests.test_browser_fixtures import PAGES

# A pseudo-class no engine knows, standing in for one newer than the engine.
_FUTURE = ":example-future"

_GROW = "@keyframes example-grow { from { opacity: 0.5 } to { opacity: 1 } }"


class ModernStylesheetTests(LayoutBrowserTests):
    """test_every_selector_parses and the settle wait, each run on a plant."""

    def planted(self, css):
        """The selector check reading ``css`` as the whole stylesheet, served
        as one of its sheets on the page it opens."""

        sheet = f"{settings.STATIC_URL.strip('/')}/{DIRECTORY}/example-planted.css"
        self.pages = {**self.pages, next(iter(PAGES)): probe(f'<link rel="stylesheet" href="/{sheet}">'), sheet: css}
        return mock.patch.object(components, "stylesheet", return_value=css)

    def selectors_parse(self):
        """The selector check itself, run on this class's plant, as a plain
        function: this class is not a ComponentLayoutTests, whose tests a
        subclass would run again."""

        vars(components.ComponentLayoutTests)["test_every_selector_parses"](self)

    def test_a_selector_the_engine_lacks_passes_behind_its_supports_guard(self):
        guarded = (
            "@supports selector(:heading) { main :heading { color: black } }\n"
            f"@media screen {{ @supports selector({_FUTURE}) {{ main {_FUTURE} {{ color: black }} }} }}"
        )
        with self.planted(guarded):
            self.selectors_parse()

    def test_a_selector_the_engine_drops_outside_a_guard_still_fails(self):
        with self.planted(f"main {_FUTURE} {{ color: black }}"), self.assertRaises(AssertionError):
            self.selectors_parse()

    def test_a_scroll_driven_animation_is_still_and_a_running_one_is_not(self):
        self.open(next(iter(PAGES)), WIDE)
        add_css(
            self.page,
            f"{_GROW}\n.example-scrolled {{ animation: example-grow 1s linear; animation-timeline: scroll(root); }}\n"
            ".example-running { animation: example-grow 600s linear; }",
        )
        self.page.evaluate(
            "() => document.querySelector('main').append(Object.assign(document.createElement('p'), {className: 'example-scrolled'}))"
        )
        self.assertTrue(self.page.evaluate(STILL))
        self.page.evaluate(
            "() => document.querySelector('main').append(Object.assign(document.createElement('p'), {className: 'example-running'}))"
        )
        self.assertFalse(self.page.evaluate(STILL))
