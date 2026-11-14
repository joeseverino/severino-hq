"""Each event the example estate writes came through the door its real
counterpart comes through, so its log reads the way real use reads."""

from collections import Counter

from django.db.models import Q
from django.test import TestCase

from hq.platform.application.security import API_INTERFACE, MCP_INTERFACE, WEB_INTERFACE
from hq.platform.core import seeds
from hq.platform.core.example_estate.agents import AGENT
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.models import AuditLog
from hq.platform.core.test_runner import TEST_INTERFACE

# The operation a test's arrangement is bound under (``test_runner.bind_test_arrangement``).
ARRANGEMENT = "test"


class ExampleEstateDoorTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def test_no_event_came_through_the_test_harness(self):
        harness = Q(interface=TEST_INTERFACE) | Q(metadata__operation=ARRANGEMENT)
        through_the_harness = Counter(
            (row.interface, row.metadata.get("operation"), row.action, row.object_type)
            for row in AuditLog.objects.filter(harness)
        )

        self.assertEqual(dict(through_the_harness), {})

    def test_what_the_owner_did_by_hand_they_did_in_the_browser(self):
        # What an agent did for them names them too, with the agent as its actor.
        by_hand = AuditLog.objects.filter(user=self.owner, actor=self.owner.get_username())
        doors = set(by_hand.values_list("interface", flat=True))

        self.assertEqual(doors, {WEB_INTERFACE})

    def test_the_agent_wrote_only_through_the_agent_doors(self):
        doors = set(AuditLog.objects.filter(actor=AGENT).values_list("interface", flat=True))

        self.assertTrue(doors)
        self.assertLessEqual(doors, {API_INTERFACE, MCP_INTERFACE})

    def test_each_thing_done_by_hand_is_a_request_of_its_own(self):
        moments: dict[str, set] = {}
        for operation, at in AuditLog.objects.filter(user=self.owner).values_list("operation_id", "created_at"):
            moments.setdefault(operation, set()).add(at.date())

        self.assertNotIn("", moments)
        self.assertEqual({operation: days for operation, days in moments.items() if len(days) > 1}, {})
