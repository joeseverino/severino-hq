"""The sites HQ publishes to the internet, read from its public records."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.projects.models import Project

from ..published_sites import public_sites


def record(name, record_type="A", content="203.0.113.10", *, enabled=True, **spec):
    return ManagedResource.objects.create(
        key=f"record-{name}-{record_type}".lower(),
        kind="cloudflare.dns_record",
        enabled=enabled,
        spec={
            "zone": "example.com",
            "name": name,
            "record_type": record_type,
            "content": content,
            "connection_ref": "example-dns",
            **spec,
        },
    )


def hostnames():
    return [hostname for hostname, _, _ in public_sites()]


class PublicSiteTests(TestCase):
    def test_a_public_record_for_a_host_is_a_site_named_by_the_project_published_there(self):
        record("Docs.Example.com.")
        record("app.example.com")
        Project.objects.create(name="Example Docs", slug="example-docs", public_url="https://docs.example.com/")

        self.assertEqual(
            public_sites(),
            (
                ("app.example.com", "", "https://app.example.com"),
                ("docs.example.com", "Example Docs", "https://docs.example.com"),
            ),
        )

    def test_a_record_that_names_no_host_is_no_site(self):
        record("mail.example.com", "TXT", "v=spf1 -all")
        record("*.example.com")
        record("_dmarc.example.com", "CNAME", "dmarc.example.net")
        record("off.example.com", enabled=False)
        ManagedResource.objects.create(
            key="example-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "example-dns"},
        )

        self.assertEqual(public_sites(), ())

    def test_a_record_its_provider_cannot_read_publishes_nothing_and_stops_nothing(self):
        broken = record("broken.example.com")
        broken.spec.pop("record_type")
        broken.save()
        record("app.example.com")

        self.assertEqual(hostnames(), ["app.example.com"])

    def test_a_name_pointed_at_another_name_published_here_is_the_same_site(self):
        record("example.com")
        record("www.example.com", "CNAME", "Example.com.")
        record("pages.example.com", "CNAME", "example-pages.example.net")

        self.assertEqual(hostnames(), ["example.com", "pages.example.com"])
