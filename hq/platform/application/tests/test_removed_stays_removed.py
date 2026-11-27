"""A record HQ removed at its provider is not taken back on from a list read before.

The stored list of a kind is read before the removal and, for a kind on its
own clock, carried unchanged by the sweeps between its reads. Every path that
writes a declaration from a stored list reads it, so the removal has to reach
the list itself.
"""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.tests.consent import consented
from hq.platform.core.testing import managing_everything

from ..controller import ControllerReport, claim_next_operation, report_operation
from ..record_errors import RecordNotFound
from ..report_testing import bridged
from ..resource_operations import OperationCommand, request_removal
from ..security import cli_principal
from ..zones import adopt_zone_records
from .zone_records import DNS_CONNECTIONS, RECORD_KIND, ZONE_KIND, ZONES, record

A_TXT = record("example.com", "TXT", '"example-verification=1"', rid="t1")


def removed(kind: str) -> None:
    """Remove the one declaration of ``kind`` the way HQ does: queued, claimed,
    and reported done by the controller."""

    resource = ManagedResource.objects.get(kind=kind)
    request_removal(OperationCommand(), consented=consented(cli_principal()), current_key=resource.key)
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


class RemovedZoneRecordTests(TestCase):
    def setUp(self):
        managing_everything(*DNS_CONNECTIONS)
        bridged("inventory", {ZONE_KIND: {"ok": True, "records": ZONES}, RECORD_KIND: {"ok": True, "records": [A_TXT]}})
        removed(RECORD_KIND)

    def test_adopting_the_domain_records_finds_nothing_to_adopt(self):
        with self.assertRaises(RecordNotFound):
            adopt_zone_records("example.com", principal=cli_principal())
