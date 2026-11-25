"""What the Connections page and its neighbors call a connection, its state and what can be done to it."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.domains.control_plane.zone_views import _pin_action
from hq.platform.application.markup_testing import text
from hq.platform.core.testing import connection, operator

from ..action_links import _CONNECTION_ROUTES
from ..connection_lifecycle import Lifecycle
from ..entity_links import entity_link


class NameTests(TestCase):
    def test_a_connection_is_called_by_the_service_it_reaches(self):
        connection("portainer", "example-portainer", endpoint="https://service.example.com")

        link = entity_link("connection", "example-portainer")

        self.assertEqual(link.label, CONNECTION_LABELS["portainer"])
        self.assertEqual(link.title, "example-portainer")

    def test_two_connections_to_one_service_are_told_apart_by_their_names(self):
        connection("cloudflare_dns", "example-dns", endpoint="https://service.example.com")
        connection("cloudflare_dns", "example-zones", endpoint="https://service.example.com")

        labels = {ref: entity_link("connection", ref).label for ref in ("example-dns", "example-zones")}

        self.assertEqual(len(set(labels.values())), 2)
        for ref, label in labels.items():
            with self.subTest(ref):
                self.assertIn(CONNECTION_LABELS["cloudflare_dns"], label)
                self.assertIn(ref, label)

    def test_a_connection_no_service_claims_keeps_its_own_name(self):
        connection("", "example-mystery", endpoint="https://service.example.com")

        self.assertEqual(entity_link("connection", "example-mystery").label, "example-mystery")

    def test_the_connections_page_heads_a_row_by_its_service(self):
        connection("portainer", "example-portainer", endpoint="https://service.example.com")
        operator(self.client)

        page = text(self.client.get(reverse("control_plane:connections")))

        self.assertIn(CONNECTION_LABELS["portainer"], page)
        self.assertIn(f"Oldest reading {CONNECTION_LABELS['portainer']}", page)


class WordTests(SimpleTestCase):
    def test_no_state_is_said_in_the_word_of_something_to_do(self):
        verbs = {label for _name, label, _field in _CONNECTION_ROUTES}

        self.assertFalse({state.label for state in Lifecycle} & verbs)

    def test_a_domain_is_starred_in_the_word_the_services_list_uses(self):
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                action = _pin_action("example.com", pinned)
                self.assertIn("favorite", action.label.lower())
