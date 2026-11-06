"""A provider's live lookup that fails is logged, and its form still renders."""

from unittest import mock

from django.test import SimpleTestCase

from hq.platform.application import provider_forms


def _failing(*args):
    raise RuntimeError("the lookup's source answered nonsense")


class FailingLookupTests(SimpleTestCase):
    def test_a_failing_lookup_is_logged_and_offers_nothing(self):
        provider = mock.Mock(kind="example.kind", choices="example:choices")
        with (
            mock.patch.object(provider_forms, "import_reference", return_value=_failing),
            self.assertLogs("severino.providers", "ERROR") as logged,
        ):
            self.assertEqual(provider_forms._live(provider, provider.choices, None), {})

        (said,) = logged.output
        self.assertIn("example.kind", said)
