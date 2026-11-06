"""A record switched off in HQ is still HQ's: a sweep does not take it on twice.

Switching a declaration off pauses HQ's changes to its record. Read as
unmanaged, the live record would be adopted again under a new key, enabled,
which undoes the switch-off.
"""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource

from ..adoption import unmanaged
from ..adoption_testing import managing_everything
from ..security import cli_principal
from ..sweep import record_sweep
from ..zones import find_zone
from .inventory_records import A_REWRITE, a_sweep
from .zone_records import DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, ZONES, record

A_TXT = record("example.com", "TXT", '"example-verification=1"', rid="t1")


def switched_off(kind: str) -> ManagedResource:
    resource = ManagedResource.objects.get(kind=kind)
    resource.enabled = False
    resource.save(update_fields=["enabled"])
    return resource


class SwitchedOffRewriteTests(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything()
        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())
        self.resource = switched_off("adguard.rewrite")

    def test_a_sweep_that_lists_it_adopts_no_second_declaration(self):
        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())

        self.assertEqual(
            list(ManagedResource.objects.filter(kind="adguard.rewrite").values_list("key", "enabled")),
            [(self.resource.key, False)],
        )
        self.assertEqual([item for item in unmanaged() if item.kind == "adguard.rewrite"], [])


class SwitchedOffZoneRecordTests(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything(*DNS_CONNECTIONS)
        self.sweep = {ZONE_KIND: {"ok": True, "records": ZONES}, RECORD_KIND: {"ok": True, "records": [A_TXT]}}
        record_sweep(self.sweep, principal=cli_principal())
        self.resource = switched_off(RECORD_KIND)

    def test_a_sweep_that_lists_it_adopts_no_second_declaration(self):
        record_sweep(self.sweep, principal=cli_principal())

        self.assertEqual(
            list(ManagedResource.objects.filter(kind=RECORD_KIND).values_list("key", "enabled")),
            [(self.resource.key, False)],
        )

    def test_the_domain_still_lists_it_as_declared(self):
        record_sweep(self.sweep, principal=cli_principal())

        listed = [entry for entry in find_zone("example.com").records if entry.record_type == "TXT"]
        self.assertEqual([entry.resource_key for entry in listed], [self.resource.key])
