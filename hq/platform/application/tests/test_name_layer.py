"""Whether a request's name is out of public DNS is a question only a name can answer."""

from unittest import mock

from django.test import SimpleTestCase

from ..request_channel import TAILNET_CHANNEL
from ..request_identity import Identity
from ..request_standard import RequestFacts, _name
from ..standards import Standing


class NameLayerTests(SimpleTestCase):
    def reading(self, host):
        facts = RequestFacts(
            request=None, address="192.0.2.10", channel=TAILNET_CHANNEL, identity=Identity(), host=host
        )
        with mock.patch("hq.platform.application.request_standard.public_answers_for", return_value=()) as answers:
            return _name(facts), answers

    def test_a_request_by_address_is_not_judged_by_dns(self):
        for host in ("192.0.2.10:8000", "[2001:db8::1]:8000", "not a name!", ""):
            with self.subTest(host=host):
                reading, answers = self.reading(host)

                self.assertIs(reading.standing, Standing.UNAVAILABLE)
                answers.assert_not_called()

    def test_a_name_with_no_public_record_holds(self):
        reading, answers = self.reading("hq.example.test:8000")

        self.assertIs(reading.standing, Standing.MET)
        answers.assert_called_once_with("hq.example.test")

    def test_a_name_with_a_public_record_does_not(self):
        facts = RequestFacts(
            request=None, address="192.0.2.10", channel=TAILNET_CHANNEL, identity=Identity(), host="hq.example.test"
        )
        with mock.patch("hq.platform.application.request_standard.public_answers_for", return_value=("203.0.113.7",)):
            reading = _name(facts)

        self.assertIs(reading.standing, Standing.UNMET)
        self.assertIn("203.0.113.7", reading.evidence)
