"""dev:look's popovers: each kind is shown on its own and judged only for what
it adds to the page."""

from django.test import SimpleTestCase, tag

from . import look, look_report
from .browser import gate


class _Popovers:
    """A page's popovers as the look's scripts reach them: some shown, one
    that will not show."""

    def __init__(self, ids, refused=(), kinds=None):
        self.ids, self.refused, self.shown = ids, set(refused), set()
        self.kinds = kinds or ids

    def evaluate(self, script, place=None):
        if script == gate.POPOVER_KINDS:
            return list(self.kinds)
        popover = self.ids[place]
        if script == gate.SHOW_POPOVER:
            if popover in self.refused:
                return None
            self.shown.add(popover)
            return [f"#{popover}", popover, True]
        self.shown.discard(popover)
        return None


class _Lens:
    """The look's walk over popovers, with a rule that finds a box on the page
    beneath, one inside a popover, and two popovers lying over each other."""

    with_each_popover = look.Look.with_each_popover
    each_popover_kind = gate.BrowserGate.each_popover_kind
    popover_shown = gate.BrowserGate.popover_shown
    # A look given no --eval runs no script.
    script = ""
    shots_into = "var/look"

    def __init__(self, page):
        self.page = page

    def take_shot(self, _name, _width, take, state="", shows=""):
        take()

    def settle(self):
        pass

    def shoot_popover(self, _name, _width, _popover, _shows):
        return []

    def judged(self, _name, _width, _rule):
        shown = sorted(self.page.shown)
        found = ["frames: a box beneath"]
        found += [f"overlaps: {one} over {other}" for one in shown for other in shown if one < other]
        return found + [f"cut: a key in {popover}" for popover in shown]


@tag("source")
class PopoverTests(SimpleTestCase):
    def test_each_popover_is_judged_alone_and_only_for_what_it_adds_to_the_page(self):
        page = _Popovers(["origin-one", "origin-two", "origin-three"], refused=["origin-three"])
        lens = _Lens(page)
        found = lens.with_each_popover("example/one", 820, "survey", ["frames: a box beneath"])
        self.assertEqual(
            found,
            [
                "with #origin-one shown: cut: a key in origin-one",
                "with #origin-two shown: cut: a key in origin-two",
            ],
        )
        self.assertEqual(page.shown, set())

    def test_against_a_base_a_finding_with_a_popover_shown_is_neither_new_nor_fixed_wherever_its_shot_lies(self):
        def found_shooting_into(folder):
            lens = _Lens(_Popovers(["origin-one", "origin-two"]))
            lens.shots_into = folder
            return {("example/one", 820): lens.with_each_popover("example/one", 820, "survey", [])}

        # A base's shots lie under the run's base folder, outside the checkout, so its path is whole.
        base = found_shooting_into("/var/folders/example/look/base")
        new, fixed = look_report.compare(base, found_shooting_into("var/look/example"))
        self.assertEqual((new, fixed), ({}, {}))

    def test_one_popover_stands_for_its_kind_and_the_next_stands_in_for_one_that_will_not_show(self):
        page = _Popovers(
            ["origin-one", "origin-two", "origin-three", "origin-four"],
            refused=["origin-one"],
            kinds=["origin-panel 0 2", "origin-panel 0 2", "origin-panel 0 2", "origin-panel 1 0"],
        )
        found = _Lens(page).with_each_popover("example/one", 820, "survey", ["frames: a box beneath"])
        self.assertEqual(
            found,
            [
                "with #origin-two and 2 like it shown: cut: a key in origin-two",
                "with #origin-four shown: cut: a key in origin-four",
            ],
        )
