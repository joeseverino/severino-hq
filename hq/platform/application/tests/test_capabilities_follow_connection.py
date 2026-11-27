"""What HQ offers to do with a resource follows whether its connection manages."""

from django.test import TestCase

from hq.platform.core.testing import connection, declare


class EveryWritePathFollowsTheConnectionTests(TestCase):
    """The capability shown on a page is the rule every write path enforces."""

    def setUp(self):
        from ..security import cli_principal

        self.principal = cli_principal()
        self.resource = declare(
            "app-rewrite",
            "adguard.rewrite",
            {"domain": "app.example.com", "answer": "10.0.0.10"},
            enabled=True,
            generation=2,
            observed_generation=1,
        )

    def test_the_scheduler_leaves_it_alone(self):
        from hq.domains.control_plane.models import OperationRequest

        from ..controller import schedule_automatic_operations

        connection("adguard", manages=False)
        schedule_automatic_operations("example-controller")

        self.assertFalse(OperationRequest.objects.filter(resource=self.resource).exists())
