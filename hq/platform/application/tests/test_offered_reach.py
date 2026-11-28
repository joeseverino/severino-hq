"""HQ offers an action, or says a thing can change something, only where the
check the action meets lets it through: a playbook where the runner reaches,
and a connection's changes from its own reads, never its provider's."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.core import seeds
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.testing import OPERATOR

# The machine the seed's playbook worker has no login to, and the shell into
# a web host whose sweep reads no Caddy route, as the "unreached" seed names them.
PRINTER = "office-printer"
WEB_HOST = "web-host"


class OfferedReachTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("unreached") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def test_a_machine_the_runner_has_no_login_to_is_offered_no_playbook_and_named_by_none(self):
        from ..automation_runs import asks_nothing, capability_name, operation_registry
        from ..capabilities import run_capability
        from ..playbook_reads import get_playbook

        offered = self.client.get(reverse("control_plane:machine", kwargs={"name": PRINTER})).context
        self.assertEqual(tuple(offered["playbooks_here"]), ())
        self.assertTrue(
            self.client.get(reverse("control_plane:machine", kwargs={"name": MACHINE})).context["playbooks_here"]
        )

        manifests = [each for each in operation_registry().values() if "machine" in each.target_kinds]
        self.assertTrue(manifests)
        for manifest in manifests:
            read = get_playbook(manifest.id, principal=OPERATOR)
            runs_on = {
                name for fact in read["facts"] if fact["label"] == "Runs on" for name in fact["value"].split(", ")
            }
            self.assertNotIn(PRINTER, read["reachable"], manifest.id)
            self.assertEqual(runs_on, set(read["reachable"]), manifest.id)

        runnable = next(each for each in manifests if asks_nothing(each) and not each.hosts)
        refused = run_capability(
            capability_name(runnable.id),
            {"hosts": [PRINTER], "parameters": {}},
            principal=OPERATOR,
            idempotency_key="example-run-printer",
        ).answer
        self.assertFalse(refused["ok"], refused)
