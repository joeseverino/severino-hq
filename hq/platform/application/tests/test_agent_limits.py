"""What one agent may ask of HQ: its rate and burst, and its calls at once, its own and everyone's."""

from django.test import SimpleTestCase, override_settings

from ..agent_limits import Limits, Slot
from ..refusal_codes import Code


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@override_settings(SEVERINO_AGENT_CALLS_AT_ONCE=2, SEVERINO_AGENT_CALLS_AT_ONCE_OVERALL=3)
class AtOnceTests(SimpleTestCase):
    def setUp(self):
        self.limits = Limits(Clock())

    def test_a_call_given_back_twice_frees_one_place(self):
        slot = self.limits.occupy("example-agent")
        held = self.limits.occupy("example-agent")
        with slot:
            pass
        with slot:
            pass

        self.assertIsInstance(self.limits.occupy("example-agent"), Slot)
        self.assertIsInstance(held, Slot)
        self.assertEqual(self.limits.occupy("example-agent").code, Code.TOO_MANY_CALLS)
