"""What the estate is due, as the calendar titles it: each says what runs out."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from ..calendar_sources import certificate_events, registration_events
from ..estate import Expiry

SUBJECT = "example.com"
TODAY = date(2026, 10, 14)


def _estate() -> SimpleNamespace:
    """One domain with every shape of certificate and registration the
    estate holds: managed or provider-renewed, auto-renewing or not, or not
    known."""

    expires = timezone.now() + timedelta(days=40)
    return SimpleNamespace(
        operator_certificates=(
            Expiry(subject=SUBJECT, source="Example CA", expires=expires, resource_key="example-cert"),
            Expiry(subject=SUBJECT, source="Example CA", expires=expires),
        ),
        registrations=(
            Expiry(subject=SUBJECT, source="Example Registrar", expires=expires, auto_renew=True),
            Expiry(subject=SUBJECT, source="Example Registrar", expires=expires, auto_renew=False),
            Expiry(subject=SUBJECT, source="Example Registrar", expires=expires),
        ),
    )


class ExpiryTitleTests(SimpleTestCase):
    def test_each_title_names_its_subject_and_what_of_it_runs_out(self):
        with mock.patch("hq.platform.application.calendar_sources.estate_reading", _estate):
            titled = {
                "certificate": certificate_events.__wrapped__(TODAY, TODAY),
                "registration": registration_events.__wrapped__(TODAY, TODAY),
            }
        for what, events in titled.items():
            self.assertTrue(events, f"no {what} on the calendar")
            for event in events:
                with self.subTest(what=what, title=event.title):
                    self.assertIn(SUBJECT, event.title)
                    self.assertIn(what, event.title)
