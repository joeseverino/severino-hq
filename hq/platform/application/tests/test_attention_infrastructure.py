from django.test import TestCase
from django.utils import timezone

from hq.domains.control_plane.models import ManagedResource

from ..attention import infrastructure
from ..findings import estate_findings
from ..security import cli_principal
from .inventory_records import kind_read


def rewrite(key: str, *, generation: int = 1, observed_generation: int = 1, **fields) -> ManagedResource:
    kind_read("adguard.rewrite", timezone.now())
    return ManagedResource.objects.create(
        key=key,
        kind="adguard.rewrite",
        spec={"domain": f"{key}.example.com", "answer": "192.0.2.1"},
        generation=generation,
        observed_generation=observed_generation,
        **{"last_observed_at": timezone.now(), **fields},
    )


def keys_about(key: str) -> set[str]:
    return {item.key for item in infrastructure() if item.key.endswith(f"resource:{key}")}


class InfrastructureQueueTests(TestCase):
    """Each matter about a resource reaches the queue once, and only once HQ
    can speak for the resource."""

    def test_a_finding_about_a_resource_waiting_to_apply_waits_with_it(self):
        rewrite("waiting", generation=2, observed_generation=1)
        found = {finding.subject for finding in estate_findings(principal=cli_principal())}

        self.assertIn("resource:waiting", found)
        self.assertEqual(keys_about("waiting"), set())

    def test_a_resource_a_finding_speaks_for_has_no_card_of_its_own(self):
        rewrite("broken", conditions=[{"type": "Degraded", "status": True, "reason": "Failed", "message": "It broke"}])

        about = keys_about("broken")

        self.assertTrue(about)
        self.assertTrue(all(key.startswith("finding:") for key in about), about)

    def test_a_resource_read_without_a_state_of_its_own_is_a_finding(self):
        rewrite("silent", status={"domain": "silent.example.com", "answer": "192.0.2.1"})

        self.assertEqual(keys_about("silent"), {"finding:resource-unchecked:resource:silent"})
        (finding,) = estate_findings(principal=cli_principal(), rule="resource-unchecked")
        self.assertIn("could not be checked", finding.title)

    def test_a_resource_never_found_is_said_so_once(self):
        """Never found in a read of its kind is the sharper claim."""

        rewrite("missing", last_observed_at=None)

        self.assertEqual(keys_about("missing"), {"finding:never-observed:resource:missing"})

    def test_a_resource_of_a_kind_never_read_is_said_once_about_the_kind(self):
        ManagedResource.objects.create(
            key="unread",
            kind="adguard.rewrite",
            spec={"domain": "unread.example.com", "answer": "192.0.2.2"},
            observed_generation=1,
        )

        rules = {finding.rule for finding in estate_findings(principal=cli_principal())}

        self.assertIn("kind-never-swept", rules)
        self.assertNotIn("resource-unchecked", rules)
