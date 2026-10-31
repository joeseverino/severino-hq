"""The observed facts a topology rule reads off a resource's own status."""

from types import SimpleNamespace

from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory

from ..topology_facts import add_observed_facts

OPERATOR = "someone@example.test"


def a_resource(key, status, *, kind="example.resource"):
    return SimpleNamespace(key=key, kind=kind, spec={}, status=status, conditions=[])


def a_tailnet():
    """HQ's machine, which took the reading, and an edge that admits it on 80 alone."""

    ProviderInventory.objects.update_or_create(
        kind="tailscale.device",
        defaults={
            "records": [
                {"name": "a-controller", "user": OPERATOR, "addresses": ["100.64.0.1"], "self": True},
                {
                    "name": "an-edge",
                    "addresses": ["100.64.0.2"],
                    "reach": [
                        {"port": 80, "who": [OPERATOR], "rules": []},
                        {"port": 443, "who": ["tag:admin"], "rules": []},
                    ],
                },
            ],
            "observed_at": timezone.now(),
        },
    )


class UnreachableConsumerTests(TestCase):
    def test_each_consumer_a_sweep_could_not_reach_is_a_fact(self):
        resource = a_resource(
            "a-certificate",
            {"unreachable_consumers": [{"domain": "app.example.com"}, {"consumer": "an-edge"}, {"domain": " "}, "x"]},
        )

        found = add_observed_facts((resource,))

        self.assertEqual(
            found["resource:a-certificate"], (("unreachable", "app.example.com"), ("unreachable", "an-edge"))
        )

    def test_a_status_naming_none_says_nothing(self):
        resources = (
            a_resource("no-status", None),
            a_resource("not-a-list", {"unreachable_consumers": "app.example.com"}),
            a_resource("nameless", {"unreachable_consumers": [{"endpoint": "100.64.0.2", "port": "443"}]}),
        )

        self.assertEqual(add_observed_facts(resources), {})


class PolicyVerdictTests(TestCase):
    def facts(self, *consumers):
        a_tailnet()
        resource = a_resource("a-certificate", {"unreachable_consumers": list(consumers)})
        return dict(add_observed_facts((resource,)).get("resource:a-certificate", ()))

    def test_a_path_the_policy_refuses_says_so(self):
        found = self.facts({"domain": "app.example.com", "endpoint": "100.64.0.2", "port": "443"})

        self.assertEqual(found["path-denied"], "a-controller to an-edge on 443")

    def test_a_path_the_policy_admits_or_cannot_judge_says_nothing_of_it(self):
        for consumer in (
            {"domain": "app.example.com", "endpoint": "100.64.0.2", "port": "80"},
            {"domain": "app.example.com", "endpoint": "100.64.0.2", "port": "8443"},
            {"domain": "app.example.com", "endpoint": "100.64.0.2", "port": "https"},
            {"domain": "app.example.com", "endpoint": "100.64.0.2"},
            {"domain": "app.example.com", "endpoint": "192.0.2.9", "port": "443"},
        ):
            with self.subTest(**consumer):
                found = self.facts(consumer)

                self.assertEqual(found["unreachable"], "app.example.com")
                self.assertNotIn("path-denied", found)
