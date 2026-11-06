"""A zone card that fails is logged, and the page keeps the rest."""

from unittest import mock

from django.test import SimpleTestCase

from hq.platform.application import zones


def _failing(zone):
    raise RuntimeError("the card's source answered nonsense")


def _answering(zone):
    return "an insight"


class FailingInsightTests(SimpleTestCase):
    def test_a_failing_card_is_logged_and_the_others_are_kept(self):
        found = {"example:failing": _failing, "example:answering": _answering}
        with (
            mock.patch.object(zones, "ZONE_INSIGHTS", tuple(found)),
            mock.patch.object(zones.plugins, "import_reference", found.__getitem__),
            self.assertLogs("severino.zones", "ERROR") as logged,
        ):
            insights = zones.Zone("example.com").insights

        self.assertEqual(insights, ("an insight",))
        (said,) = logged.output
        self.assertIn("example:failing", said)
        self.assertIn("example.com", said)
