"""axe-core's rules as the survey holds them, on probe pages planted with what each must find and pass."""

from typing import override

from hq.platform.core.tests.browser.gate import with_script
from hq.platform.core.tests.browser.layout import LayoutBrowserTests
from hq.platform.core.tests.browser.probes.axe import contrast_read
from hq.platform.core.tests.browser.reading import probe
from hq.platform.core.tests.browser.widths import WIDE

# What axe must find: text too faint to read, a button with no name and a
# field with no label.
PLANTED = "accessibility/planted"

# The same faint text where WCAG leaves it out, inside a control disabled or
# aria-disabled, and text that clears 4.5:1, plain and faded by an opacity.
EXEMPT = "accessibility/exempt"

# Text that reads in its ink, faded by an opacity until it does not.
FADED = "accessibility/faded"

# Ink the color of its ground, and one faint character, which axe leaves to a
# person rather than calling a violation.
UNREAD = "accessibility/unread"

_FAINT = "color: #c8c8c8; background: #fff"

_PAGES = {
    PLANTED: f"""<p class="example-faint" style="{_FAINT}">Example words too faint to read</p>
<button type="button" class="example-unnamed"></button>
<input type="text" name="example" class="example-unlabeled">""",
    EXEMPT: f"""<button type="button" class="example-disabled" disabled style="{_FAINT}">Example control not in play</button>
<span class="example-dead-end" aria-disabled="true" style="{_FAINT}; opacity: .35">Example end of a range</span>
<p class="example-clear" style="color: #333; background: #fff">Example words that read</p>
<p class="example-faded-clear" style="color: #333; background: #fff; opacity: .9">Example words faded that still read</p>""",
    FADED: """<p style="background: #fff"><a class="example-faded" href="#example" style="color: #595959; opacity: .62">Example day gone by</a></p>""",
    UNREAD: """<p class="example-unseen" style="color: #fff; background: #fff">Example words drawn in their ground</p>
<p style="background: #fff"><span class="example-lone" style="color: #c8c8c8">x</span></p>""",
}


class AccessibilityBrowserTests(LayoutBrowserTests):
    """The survey's axe checks find what is planted for them, under the policy
    HQ answers every page with, with script off and on, in the engine the
    gate drives."""

    @classmethod
    @override
    def render(cls):
        return {name: probe(markup) for name, markup in _PAGES.items()}

    def survey(self, name):
        self.open(name, WIDE)
        return self.findings(name, WIDE)

    def assert_planted_found(self):
        found = self.survey(PLANTED)
        self.assertEqual([line.split(" ")[0] for line in found.get("contrast", [])], ["p.example-faint"])
        said = "\n".join(found.get("axe", []))
        self.assertIn("button-name: button.example-unnamed", said)
        self.assertIn("label: input.example-unlabeled", said)

    def test_the_survey_finds_what_axe_is_planted_with_script_off(self):
        """axe is evaluated as every probe is, so a page whose policy admits
        only its own nonced scripts still runs it, and an engine with script
        off still settles it. The policy's refusals fail the test as it ends."""

        self.assert_planted_found()

    @with_script
    def test_the_survey_finds_what_axe_is_planted_with_script_on(self):
        self.assert_planted_found()

    def test_an_inactive_control_is_not_held_to_contrast_and_the_same_ink_in_play_is(self):
        """WCAG leaves out an inactive component, and nothing else: text that
        clears 4.5:1 passes, faded or not, and the faint ink is held once its
        control is in play."""

        self.open(EXEMPT, WIDE)
        self.assertEqual(self.read([contrast_read()])[0], [])
        self.page.evaluate(
            "() => document.querySelectorAll('main *').forEach((el) => { el.disabled = false; el.removeAttribute('aria-disabled'); })"
        )
        found = [line.split(" ")[0] for line in self.read([contrast_read()])[0]]
        self.assertEqual(sorted(found), ["button.example-disabled", "span.example-dead-end"])

    def test_text_in_play_faded_by_an_opacity_is_held_as_it_is_drawn(self):
        """A link a reader can follow is in play however faint it is drawn,
        and its ink clears 4.5:1 only before the fade."""

        self.open(FADED, WIDE)
        found = [line.split(" ")[0] for line in self.read([contrast_read()])[0]]
        self.assertEqual(found, ["a.example-faded"])

    def test_ink_the_color_of_its_ground_and_a_lone_faint_character_are_found(self):
        """axe leaves both to a person; a reader sees nothing and a smudge."""

        self.open(UNREAD, WIDE)
        found = [line.split(" ")[0] for line in self.read([contrast_read()])[0]]
        self.assertEqual(sorted(found), ["p.example-unseen", "span.example-lone"])
