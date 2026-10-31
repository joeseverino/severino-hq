"""A command that needs what only one page hands out is started from that page."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.capabilities import capability_registry
from hq.platform.application.command_center import command_center
from hq.platform.application.command_targets import starting_page
from hq.platform.application.core_capabilities import CORE_CAPABILITY_SPECS
from hq.platform.application.integration_specs import ON_ITS_PAGE
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.markup import select, within
from hq.platform.core.models import Appearance

User = get_user_model()
OPERATOR = Principal("operator", "web", frozenset(Capability))


def commands_found(query: str) -> dict:
    return {item.name: item for item in command_center(query, principal=OPERATOR)["commands"]}


class CommandStartTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="command-starts",
            email="operator@example.test",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_every_starting_page_is_a_named_page(self):
        for spec in capability_registry().values():
            if not spec.reached_from or spec.reached_from == ON_ITS_PAGE:
                continue
            with self.subTest(command=spec.name):
                label, url = starting_page(spec)
                self.assertTrue(label.strip())
                self.assertEqual(url, reverse(spec.reached_from))

    def test_search_links_a_page_held_command_to_its_page(self):
        approve = commands_found("approval.approve")["approval.approve"]

        self.assertEqual(approve.url, reverse("action_items"))
        self.assertTrue(approve.destination_label)

    def test_search_does_not_offer_a_command_only_its_own_page_sends(self):
        held_by_their_pages = {spec.name for spec in CORE_CAPABILITY_SPECS if spec.reached_from == ON_ITS_PAGE}

        self.assertTrue(held_by_their_pages)
        for name in held_by_their_pages:
            with self.subTest(command=name):
                self.assertTrue(OPERATOR.permits(*capability_registry()[name].required_capabilities))
                self.assertNotIn(name, commands_found(name))

    def test_a_command_with_no_starting_page_still_links_its_form(self):
        refresh = commands_found("infrastructure.controller.refresh")["infrastructure.controller.refresh"]

        self.assertEqual(refresh.url, reverse("command", args=["infrastructure.controller.refresh"]))

    def test_the_form_of_a_page_held_command_points_to_its_page(self):
        page = self.client.get(reverse("command", args=["approval.approve"]))

        (notice,) = select(page, "data-command-start")
        links = [node.attrs.get("href") for node in within(page, "data-command-start") if node.tag == "a"]
        self.assertIsNotNone(notice)
        self.assertIn(reverse("action_items"), links)

    def test_a_command_with_a_form_of_its_own_has_no_pointer(self):
        page = self.client.get(reverse("command", args=["infrastructure.controller.refresh"]))

        self.assertEqual(select(page, "data-command-start"), [])

    def test_the_theme_is_chosen_from_the_declared_themes(self):
        page = self.client.get(reverse("command", args=["appearance.theme.set"]))

        offered = {node.attrs["value"] for node in within(page, "name", "theme") if node.tag == "option"}
        self.assertEqual(offered, set(Appearance.Theme.values))
