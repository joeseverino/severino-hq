"""A past expiry reads as past on every surface that says when something runs out."""

import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from hq.domains.control_plane.models import ManagedResource

from ..certificate_expiry import CERTIFICATE_EXPIRES, expiring as certificates_expiring
from ..credential_findings import EXPIRES, expiring as credentials_expiring
from ..registration_findings import RULES as REGISTRATION_RULES
from ..tailnet_presence import Presence
from ..zones import find_zone
from .zone_records import sweep

NOW = datetime(2026, 1, 10, 12, tzinfo=UTC)
# A future, the moment itself, earlier the same day, and days past, each with
# what every surface says of it.
SAID = {
    timedelta(days=3): "expires in 3 days",
    timedelta(0): "expires today",
    -timedelta(hours=2): "expired today",
    -timedelta(days=4, hours=1): "expired 4 days ago",
}
# What no expiry phrase may say: a count below nothing, or an unagreeing unit.
NEGATIVE = re.compile(r"-\s?\d|\b1 days\b")


def at_now():
    return mock.patch("hq.platform.application.derivations.clock", return_value=NOW)


def node(**facts) -> SimpleNamespace:
    return SimpleNamespace(
        id="connection:example-edge",
        kind="connection",
        label="example-edge",
        facts=tuple(facts.items()),
    )


def estate(*nodes) -> SimpleNamespace:
    return SimpleNamespace(now=NOW, nodes=lambda: nodes)


class ExpiryWordsTests(SimpleTestCase):
    def test_each_tense_is_its_own_phrase(self):
        from ..moments import expiry_words

        cases = {
            **SAID,
            timedelta(hours=20): "expires in 1 day",
            -timedelta(days=1, hours=2): "expired 1 day ago",
        }
        for offset, said in cases.items():
            with self.subTest(offset=offset):
                self.assertEqual(expiry_words(NOW + offset, NOW), said)

    def test_a_count_of_days_reads_as_its_moment_does(self):
        from ..expiry import days_until
        from ..moments import expiry_words

        for offset in SAID:
            when = NOW + offset
            with self.subTest(offset=offset):
                self.assertEqual(expiry_words(days_until(when, NOW)), expiry_words(when, NOW))


class EveryCallerTests(TestCase):
    """Each surface says the phrase ``expiry_words`` gives for its moment."""

    def assertSays(self, read) -> None:
        """``read(when)``, the caller's text for a moment, says each moment as ``SAID`` does."""

        for offset, said in SAID.items():
            with self.subTest(offset=offset), at_now():
                text = read(NOW + offset)
                self.assertIn(said, text)
                self.assertNotRegex(text, NEGATIVE)

    def test_a_certificate_finding(self):
        def read(when):
            fact = "|".join(("", "example.com", when.isoformat(), "app.example.com"))
            (finding,) = certificates_expiring(estate(node(**{CERTIFICATE_EXPIRES: fact})))
            return finding["title"]

        self.assertSays(read)

    def test_a_credential_finding(self):
        def read(when):
            (finding,) = credentials_expiring(estate(node(**{EXPIRES: when.isoformat()})))
            return finding["title"]

        self.assertSays(read)

    def test_a_registration_finding(self):
        (rule,) = REGISTRATION_RULES

        def read(when):
            facts = {"expires_at": when.isoformat(), "auto_renew": "no", "domain": "example.com"}
            (finding,) = rule.detect(estate(node(**facts)))
            return finding.title

        self.assertSays(read)

    def test_an_estate_certificate_item(self):
        from ..estate import Expiry, _expiring

        def read(when):
            (item,) = _expiring(SimpleNamespace(operator_certificates=(Expiry("example.com", "Cloudflare", when),)))
            return item.title

        self.assertSays(read)

    def test_a_requests_device_layer(self):
        from ..request_layers import _expiry_phrase

        self.assertSays(lambda when: _expiry_phrase(when.isoformat()))

    def test_a_machines_node_key(self):
        self.assertSays(lambda when: Presence(key_expires=when.isoformat()).key_expiry)

    @override_settings(SEVERINO_INFRASTRUCTURE_ENABLE_PUBLIC_DNS=True)
    def test_a_domains_security_card(self):
        from ..zone_insights import security

        sweep()
        certificate = ManagedResource.objects.create(
            key="wildcard",
            kind="tls.certificate",
            spec={"certificate_name": "wildcard", "domains": ["example.com"], "install_on": ["edge"]},
            enabled=True,
        )

        def read(when):
            certificate.status = {"not_after": when.isoformat()}
            certificate.save()
            return security(find_zone("example.com")).detail

        self.assertSays(read)
