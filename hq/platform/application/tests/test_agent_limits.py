"""What one agent may ask of HQ: its rate and burst, and its calls at once, its own and everyone's."""

from django.test import SimpleTestCase, override_settings

from ..agent_limits import RATE_LIMITED, TOO_MANY_CALLS, Limited, Limits, Slot


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@override_settings(SEVERINO_AGENT_CALLS_PER_MINUTE=60, SEVERINO_AGENT_CALL_BURST=3)
class RateTests(SimpleTestCase):
    def setUp(self):
        self.clock = Clock()
        self.limits = Limits(self.clock)

    def test_an_agent_spends_its_burst_at_once_and_is_then_told_when_to_ask_again(self):
        self.assertEqual([self.limits.take("example-agent") for _ in range(3)], [None, None, None])

        refused = self.limits.take("example-agent")

        self.assertIsInstance(refused, Limited)
        self.assertEqual(refused.code, RATE_LIMITED)
        self.assertGreaterEqual(refused.retry_after, 1)
        self.clock.now += refused.retry_after
        self.assertIsNone(self.limits.take("example-agent"))

    def test_a_refused_call_spends_nothing(self):
        for _ in range(3):
            self.limits.take("example-agent")
        for _ in range(50):
            self.limits.take("example-agent")

        self.clock.now += 1
        self.assertIsNone(self.limits.take("example-agent"), "a flood of refusals does not push recovery back")

    def test_one_agent_spending_its_rate_leaves_another_its_own(self):
        for _ in range(4):
            self.limits.take("example-flood")

        self.assertIsNone(self.limits.take("example-other"))

    def test_an_agent_that_stopped_calling_holds_nothing_once_it_recovered(self):
        for _ in range(3):
            self.limits.take("example-agent")
        self.assertEqual(self.limits.held(), 1)

        self.clock.now += 3

        self.assertEqual(self.limits.held(), 0)


@override_settings(SEVERINO_AGENT_CALLS_AT_ONCE=2, SEVERINO_AGENT_CALLS_AT_ONCE_OVERALL=3)
class AtOnceTests(SimpleTestCase):
    def setUp(self):
        self.limits = Limits(Clock())

    def test_an_agent_is_held_to_its_calls_at_once_and_gets_one_back_as_one_ends(self):
        first, second = self.limits.occupy("example-agent"), self.limits.occupy("example-agent")
        self.assertIsInstance(first, Slot)
        self.assertIsInstance(second, Slot)

        refused = self.limits.occupy("example-agent")

        self.assertEqual(refused.code, TOO_MANY_CALLS)
        self.assertGreaterEqual(refused.retry_after, 1)
        with first:
            pass
        self.assertIsInstance(self.limits.occupy("example-agent"), Slot)

    def test_every_agent_together_is_held_to_the_overall_count(self):
        for actor in ("example-a", "example-a", "example-b"):
            self.assertIsInstance(self.limits.occupy(actor), Slot)

        self.assertEqual(self.limits.occupy("example-c").code, TOO_MANY_CALLS)

    def test_a_call_given_back_twice_frees_one_place(self):
        slot = self.limits.occupy("example-agent")
        held = self.limits.occupy("example-agent")
        with slot:
            pass
        with slot:
            pass

        self.assertIsInstance(self.limits.occupy("example-agent"), Slot)
        self.assertIsInstance(held, Slot)
        self.assertEqual(self.limits.occupy("example-agent").code, TOO_MANY_CALLS)

    def test_an_agent_with_nothing_in_flight_holds_nothing(self):
        with self.limits.occupy("example-agent"):
            self.assertEqual(self.limits.held(), 1)

        self.assertEqual(self.limits.held(), 0)
