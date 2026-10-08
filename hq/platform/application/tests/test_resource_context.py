from types import SimpleNamespace

from django.test import SimpleTestCase

from ..resource_context import controller_summary


def allowance(enabled: bool, reason: str = "", automatic: bool = False):
    return SimpleNamespace(enabled=enabled, reason=reason, automatic=automatic)


def label(verb: str) -> str:
    return verb.capitalize()


class ControllerSummaryTests(SimpleTestCase):
    def test_a_controller_that_can_do_nothing_is_observing_only_and_says_why_once(self):
        observes = "Its connection only observes. Set manages on the connection to act."
        found = controller_summary(
            {"reconcile": allowance(False, observes, automatic=True), "renew": allowance(False, observes, automatic=True)},
            label,
        )

        self.assertEqual(found.headline, "HQ does not change this")
        self.assertEqual(found.lines, (observes,))
        self.assertIn(observes, found.reasons)

    def test_one_that_can_act_says_how(self):
        found = controller_summary(
            {"reconcile": allowance(True, automatic=True), "renew": allowance(False, "Not due yet.")}, label
        )

        self.assertEqual((found.headline, found.tone), ("Applied automatically", "good"))
        self.assertEqual(found.lines, ("Not due yet.",))

    def test_no_actions_no_summary(self):
        self.assertIsNone(controller_summary({}, label))
