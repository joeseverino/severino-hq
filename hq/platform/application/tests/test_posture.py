"""HQ's standard: each check read from what decides it, shown always, raised
under Advanced HQ posture (``standards.Standard``, held in test_standards)."""

from django.test import TestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.core.testing import connection

from ..connection_catalog import connection_catalog
from ..inventory_testing import store
from ..posture import estate_posture
from ..security import cli_principal
from ..standards import CheckResult, Standing


def standing(check_id: str) -> CheckResult:
    found = estate_posture(connection_catalog(principal=cli_principal()))
    return next(result for result in found.results if result.check.id == check_id)


def policy(**record) -> None:
    ProviderInventory.objects.create(
        kind="tailscale.policy", observed_at=timezone.now(), records=[{"record": "policy", **record}]
    )


class ReadingTests(TestCase):
    def test_an_allow_all_acl_is_unmet(self):
        policy(grants=[], document='{"acls": [{"action": "accept", "src": ["*"], "dst": ["*:*"]}]}')

        self.assertFalse(standing("tailnet-policy").met)

    # No heartbeat: only the readings say whether a controller has run.
    @override_settings(SEVERINO_CONTROLLER_HEARTBEAT="/nonexistent/example-heartbeat")
    def test_an_unread_check_says_which_cause_holds_it(self):
        connection("tailscale", "example-tailnet")
        cases = {
            "no controller has run": lambda: None,
            "no read has arrived": lambda: store("example.other"),
            "the last read failed": lambda: store(
                "tailscale.policy", reachable=False, error="Tailscale refused the token."
            ),
            "the reading held no answer": lambda: store("tailscale.policy", {"record": "example"}),
        }
        said = {}
        for cause, arrange in cases.items():
            with self.subTest(cause=cause):
                ProviderInventory.objects.all().delete()
                arrange()

                found = standing("tailnet-policy").reading

                self.assertEqual(found.standing, Standing.UNAVAILABLE)
                said[cause] = found.evidence
        self.assertEqual(len(set(said.values())), len(cases), said)
        self.assertIn("Tailscale refused the token", said["the last read failed"])
