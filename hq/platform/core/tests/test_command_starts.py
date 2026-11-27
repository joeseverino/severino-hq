"""A command that needs what only one page hands out is started from that page."""

from django.urls import reverse

from hq.platform.application.command_center import command_center
from hq.platform.application.security import Capability, Principal
from hq.platform.core.testing import SignedInTestCase

OPERATOR = Principal("operator", "web", frozenset(Capability))


def commands_found(query: str) -> dict:
    return {item.name: item for item in command_center(query, principal=OPERATOR)["commands"]}


class CommandStartTests(SignedInTestCase):
    def test_search_links_a_page_held_command_to_its_page(self):
        approve = commands_found("approval.approve")["approval.approve"]

        self.assertEqual(approve.url, reverse("action_items"))
        self.assertTrue(approve.destination_label)
