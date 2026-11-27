"""The sites HQ publishes to the internet, read from its public records."""

from django.test import TestCase

from hq.platform.core.testing import declare

from ..published_sites import public_sites


def record(name, record_type="A", content="203.0.113.10", *, enabled=True, **spec):
    return declare(
        f"record-{name}-{record_type}".lower(),
        "cloudflare.dns_record",
        {
            "zone": "example.com",
            "name": name,
            "record_type": record_type,
            "content": content,
            "connection_ref": "example-dns",
            **spec,
        },
        enabled=enabled,
    )


def hostnames():
    return [hostname for hostname, _, _ in public_sites()]


class PublicSiteTests(TestCase):
    def test_a_record_its_provider_cannot_read_publishes_nothing_and_stops_nothing(self):
        broken = record("broken.example.com")
        broken.spec.pop("record_type")
        broken.save()
        record("app.example.com")

        self.assertEqual(hostnames(), ["app.example.com"])
