from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.core.testing import declare

from ..zones import zone_catalog

RECORD_KIND = "cloudflare.dns_record"


def declared_record(key: str, **spec) -> ManagedResource:
    return declare(key, RECORD_KIND, {"name": "www.example.net", "record_type": "A", "content": "192.0.2.7", **spec})


class DeclaredRecordPlacementTests(TestCase):
    """A declared record is listed under the domain it names."""

    def test_a_declared_record_is_listed_under_its_domain_and_one_naming_none_under_no_domain(self):
        declared_record("placed", zone="example.net")
        declared_record("loose")

        listed = {zone.zone: {record.resource_key for record in zone.records} for zone in zone_catalog()}

        self.assertEqual(listed, {"example.net": {"placed"}})
