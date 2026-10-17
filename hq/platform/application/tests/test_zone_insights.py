from types import SimpleNamespace

from django.test import SimpleTestCase

from ..zone_insights import _mid_sentence, email


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


class MidSentenceTests(SimpleTestCase):
    def test_a_word_is_lowercased_and_an_acronym_kept(self):
        self.assertEqual(_mid_sentence("Behind Access"), "behind Access")
        self.assertEqual(_mid_sentence("DNS lookups"), "DNS lookups")
