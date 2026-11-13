from types import SimpleNamespace

from django.test import SimpleTestCase

from ..zone_insights import _edge_certificates, email


def record(record_type, name, content, priority=None):
    return SimpleNamespace(record_type=record_type, name=name, content=content, priority=priority)


class EmailCardTests(SimpleTestCase):
    def test_a_null_mx_reads_as_taking_no_mail_with_no_empty_server(self):
        zone = SimpleNamespace(
            zone="example.com",
            records=(
                record("MX", "example.com", ".", 0),
                record("TXT", "example.com", "v=spf1 -all"),
                record("TXT", "_dmarc.example.com", "v=DMARC1; p=reject"),
            ),
        )

        card = email(zone)

        self.assertEqual(card.value, "Not received")
        self.assertIn("A null MX says it takes no mail.", card.detail)


class EdgeCertificateCardTests(SimpleTestCase):
    def test_a_certificate_uploaded_to_a_proxy_is_counted_apart_from_the_issuers(self):
        edge = tuple(
            SimpleNamespace(issuer=issuer, expires="2030-01-01T00:00:00+00:00")
            for issuer in ("Let's Encrypt", "uploaded")
        )

        detail = _edge_certificates(edge).detail

        self.assertIn("Issued by Let's Encrypt.", detail)
        self.assertIn("1 certificate uploaded.", detail)
