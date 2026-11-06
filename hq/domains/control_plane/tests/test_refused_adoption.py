"""A record a sweep cannot take on is said in the log, and the rest are still taken on."""

from unittest import mock

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application import adoption, zones
from hq.platform.application.adoption_testing import managing_everything
from hq.platform.application.inventory import record_inventory
from hq.platform.application.record_errors import PolicyError
from hq.platform.application.security import cli_principal

KIND = "adguard.rewrite"
REFUSED = {"domain": "app.example.com", "answer": "192.0.2.10", "enabled": True}
TAKEN = {"domain": "tool.example.com", "answer": "192.0.2.11", "enabled": True}


class RefusedAdoptionTests(TestCase):
    def setUp(self):
        managing_everything()
        record_inventory({KIND: {"ok": True, "records": [REFUSED, TAKEN]}}, principal=cli_principal())

    def test_a_record_that_cannot_be_adopted_is_logged_and_the_rest_are_adopted(self):
        adopt = adoption.adopt

        def refusing(command, *, principal):
            item = next(item for item in adoption.unmanaged() if item.token == command.token)
            if item.hostnames == ("app.example.com",):
                raise PolicyError("Public DNS is switched off here.")
            return adopt(command, principal=principal)

        with (
            mock.patch.object(adoption, "adopt", refusing),
            self.assertLogs("severino.adoption", "WARNING") as logged,
        ):
            adoption.adopt_discovered(KIND, principal=cli_principal())

        domains = {resource.spec["domain"] for resource in ManagedResource.objects.filter(kind=KIND)}
        self.assertEqual(domains, {"tool.example.com"})
        (said,) = logged.output
        self.assertIn("app.example.com", said)
        self.assertIn("Public DNS is switched off here.", said)


class RefusedZoneAdoptionTests(TestCase):
    def test_a_zone_whose_records_cannot_be_adopted_is_logged(self):
        zone = mock.Mock(zone="example.com", managed=True, adoptable=True)
        with (
            mock.patch.object(zones, "zone_catalog", return_value=(zone,)),
            mock.patch.object(zones, "adopt_zone_records", side_effect=PolicyError("Public DNS is switched off here.")),
            self.assertLogs("severino.zones", "WARNING") as logged,
        ):
            self.assertEqual(zones.adopt_discovered_records(principal=cli_principal()), {"ok": True, "adopted": []})

        (said,) = logged.output
        self.assertIn("example.com", said)
        self.assertIn("Public DNS is switched off here.", said)
