"""A record HQ removed at its provider is not taken back on from a list read before.

The stored list of a kind is read before the removal and, for a kind on its
own clock, carried unchanged by the sweeps between its reads. Every path that
writes a declaration from a stored list reads it, so the removal has to reach
the list itself.
"""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource

from ..adoption import AdoptCommand, AdoptServiceCommand, adopt, adopt_service, unmanaged
from ..adoption_testing import managing_everything
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..record_errors import RecordNotFound
from ..resource_operations import OperationCommand, request_removal
from ..security import cli_principal
from ..sweep import record_sweep
from ..zones import adopt_zone_records
from .inventory_records import A_REWRITE, a_sweep
from .zone_records import DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, ZONES, record

A_TXT = record("example.com", "TXT", '"example-verification=1"', rid="t1")
CARRIED = {"ok": True, "records": [], "carried": True}


def removed(kind: str) -> None:
    """Remove the one declaration of ``kind`` the way HQ does: queued, claimed,
    and reported done by the controller."""

    resource = ManagedResource.objects.get(kind=kind)
    request_removal(OperationCommand(idempotency_key="remove-1"), principal=cli_principal(), current_key=resource.key)
    claim = claim_next_operation("test-controller", capabilities=((kind, "delete"),))
    report_operation(
        claim["operation"]["id"],
        ControllerReport(
            success=True,
            observed_generation=resource.generation,
            status={},
            conditions=[{"type": "Ready", "status": True, "reason": "Removed", "message": ""}],
            message="",
        ),
        controller_id="test-controller",
    )
    assert not ManagedResource.objects.filter(kind=kind).exists()


class RemovedRewriteTests(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything()
        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())
        removed("adguard.rewrite")

    def test_a_sweep_that_carries_the_kind_adopts_nothing_back(self):
        record_sweep({"adguard.rewrite": CARRIED}, principal=cli_principal())

        self.assertFalse(ManagedResource.objects.filter(kind="adguard.rewrite").exists())
        self.assertEqual([item for item in unmanaged() if item.kind == "adguard.rewrite"], [])

    def test_a_sweep_of_other_kinds_adopts_nothing_back(self):
        record_sweep(a_sweep(**{"npm.proxy_host": []}), principal=cli_principal())

        self.assertFalse(ManagedResource.objects.filter(kind="adguard.rewrite").exists())

    def test_adopting_by_hand_finds_nothing_to_adopt(self):
        with self.assertRaises(RecordNotFound):
            adopt(AdoptCommand(kind="adguard.rewrite", hostname=A_REWRITE["domain"]), principal=cli_principal())
        with self.assertRaises(RecordNotFound):
            adopt_service(AdoptServiceCommand(hostname=A_REWRITE["domain"]), principal=cli_principal())

    def test_a_read_that_lists_it_again_adopts_it(self):
        """Only HQ's own removal is struck: a later read is the provider's word."""

        record_sweep(a_sweep(**{"adguard.rewrite": [A_REWRITE]}), principal=cli_principal())

        self.assertTrue(ManagedResource.objects.filter(kind="adguard.rewrite").exists())


class RemovedZoneRecordTests(TestCase):
    def setUp(self):
        super().setUp()
        managing_everything(*DNS_CONNECTIONS)
        record_sweep(
            {ZONE_KIND: {"ok": True, "records": ZONES}, RECORD_KIND: {"ok": True, "records": [A_TXT]}},
            principal=cli_principal(),
        )
        removed(RECORD_KIND)

    def test_a_sweep_that_carries_the_records_adopts_nothing_back(self):
        record_sweep({ZONE_KIND: {"ok": True, "records": ZONES}, RECORD_KIND: CARRIED}, principal=cli_principal())

        self.assertFalse(ManagedResource.objects.filter(kind=RECORD_KIND).exists())
        self.assertEqual([item for item in unmanaged() if item.kind == RECORD_KIND], [])

    def test_adopting_the_domain_records_finds_nothing_to_adopt(self):
        with self.assertRaises(RecordNotFound):
            adopt_zone_records("example.com", principal=cli_principal())
