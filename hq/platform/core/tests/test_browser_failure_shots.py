"""A page's screenshot stops where a browser can still take one and says what it left out,
and a failed test's shot that cannot be taken never stands in for the test's own failure."""

from unittest import mock

from django.test import SimpleTestCase
from django.test.runner import RemoteTestResult

from hq.platform.core.tests.browser import gate
from hq.platform.core.tests.browser.shots import TALLEST, shoot_page

# What Chromium refuses an image past on either side.
_LARGEST_IMAGE = 32_767


class _Page:
    """A page ``tall`` px high that keeps each screenshot's clip, refusing any past what Chromium takes."""

    def __init__(self, tall):
        self.tall = tall
        self.clips = []

    def evaluate(self, _script):
        return [1360, self.tall]

    def screenshot(self, *, path, full_page, clip=None, **_options):
        height = clip["height"] if clip else self.tall
        if height > _LARGEST_IMAGE:
            raise ValueError("Cannot take screenshot larger than 32767 pixels on any dimension")
        self.clips.append(clip)


class ShootPageTests(SimpleTestCase):
    def test_a_page_past_what_a_shot_holds_is_shot_to_there_and_says_so(self):
        page = _Page(4 * _LARGEST_IMAGE)

        cut = shoot_page(page, "example.png")

        self.assertLessEqual(page.clips[0]["height"], _LARGEST_IMAGE)
        self.assertIn(str(4 * _LARGEST_IMAGE), cut)
        self.assertIn(str(page.clips[0]["height"]), cut)

    def test_a_page_a_shot_holds_is_shot_whole_and_cuts_nothing(self):
        page = _Page(2400)

        self.assertEqual(shoot_page(page, "example.png"), "")
        self.assertEqual(page.clips, [{"x": 0, "y": 0, "width": 1360, "height": 2400}])

    def test_a_band_inside_the_shot_is_shot_as_asked(self):
        page = _Page(4 * _LARGEST_IMAGE)

        self.assertEqual(shoot_page(page, "example.png", band=(TALLEST - 900, 900)), "")
        self.assertEqual(page.clips, [{"x": 0, "y": TALLEST - 900, "width": 1360, "height": 900}])


class _Gone(Exception):
    pass


class _Unshootable(gate.BrowserGate):
    """A gate whose browser cannot take the failure's screenshot. Not named test_, so discovery runs it only here."""

    def start(self, **options):
        self.page = None

    def shoot(self, name):
        raise _Gone("Page.screenshot: Timeout 5000ms exceeded.")

    def breaks(self):
        self.fail("on purpose")


class FailureShotTests(SimpleTestCase):
    def test_a_shot_that_cannot_be_taken_leaves_the_tests_own_failure_as_its_report(self):
        probe = _Unshootable("breaks")
        result = RemoteTestResult()

        # The browser's own error class, without loading the browser group.
        with mock.patch.dict(vars(gate), BrowserError=_Gone), mock.patch("builtins.print") as said:
            probe.run(result)

        reported = [(event[0], str(event[2][1])) for event in result.events if event[0] in {"addError", "addFailure"}]
        self.assertEqual(reported, [("addFailure", "on purpose")])
        self.assertIn("Timeout 5000ms", " ".join(str(call) for call in said.call_args_list))
