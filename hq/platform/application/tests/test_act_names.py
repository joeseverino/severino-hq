"""Every audit row a capability's run writes carries the capability's own
name, so the log names an act by the command a person ran."""

from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.platform.core.models import AuditLog

from ..adoption_testing import managing_everything
from ..capabilities import execute_capability
from ..report_testing import bridged
from ..security import cli_principal
from .inventory_records import A_REWRITE, a_sweep

KIND = "adguard.rewrite"


class ActNameTests(TestCase):
    def setUp(self):
        managing_everything()
        bridged("inventory", a_sweep(**{KIND: [A_REWRITE]}))
        self.key = ManagedResource.objects.get(kind=KIND).key

    def test_each_row_a_run_writes_names_the_capability(self):
        name = "infrastructure.resource.reconcile"
        before = AuditLog.objects.count()

        answer = execute_capability(name, {}, principal=cli_principal(), target=self.key)

        self.assertTrue(answer["ok"], answer)
        written = AuditLog.objects.order_by("pk")[before:]
        self.assertTrue(written, "the run wrote no row, so this checks nothing")
        self.assertEqual({row.metadata["operation"] for row in written}, {name})
