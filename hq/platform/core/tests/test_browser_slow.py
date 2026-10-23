"""`HQ_BROWSER_SLOW` slows a gate's frames by what it names, and nothing
unless it is set."""

import os
from unittest import mock

from django.test import SimpleTestCase, tag

from hq.platform.core.browser_tests import slow_frames


@tag("source")
class SlowFramesTests(SimpleTestCase):
    def slowed(self, late):
        with mock.patch.dict(os.environ, {"HQ_BROWSER_SLOW": late}):
            return slow_frames()

    def test_unset_nothing_is_slowed(self):
        with mock.patch.dict(os.environ, clear=True):
            self.assertIsNone(slow_frames())
        self.assertIsNone(self.slowed(""))

    def test_the_script_is_called_with_the_milliseconds_asked_for(self):
        script = self.slowed("150")
        self.assertTrue(script.rstrip(";").endswith("(150)"))

    def test_a_delay_that_is_no_positive_number_is_refused(self):
        for late in ("0", "-5", "fast", "1.5"):
            with self.subTest(late=late), self.assertRaisesMessage(ValueError, "HQ_BROWSER_SLOW"):
                self.slowed(late)
