"""A refusal carries a wait only when its code says "later", the codes the API document declares a wait at."""

from django.test import SimpleTestCase

from hq.platform.application.agent_limits import RATE_LIMITED, TOO_MANY_CALLS
from hq.platform.application.refusal_status import LATER_CODES, Refusal


class LaterCodeTests(SimpleTestCase):
    def test_every_code_an_agent_is_told_to_wait_by_says_later(self):
        self.assertLessEqual({RATE_LIMITED, TOO_MANY_CALLS}, LATER_CODES)

    def test_a_later_code_keeps_its_wait(self):
        self.assertEqual(
            Refusal(RATE_LIMITED, "Retry in 3 seconds.", retry_after=3).answer()["error"]["retry_after"], 3
        )

    def test_a_wait_on_any_other_code_is_dropped_and_logged(self):
        with self.assertLogs("severino.capabilities", "ERROR"):
            refusal = Refusal("conflict", "Changed.", retry_after=3)
        self.assertIsNone(refusal.retry_after)
        self.assertNotIn("retry_after", refusal.answer()["error"])
