"""What the Connections page and its neighbors call a connection, its state and what can be done to it."""

from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.connection_kinds import CONNECTION_LABELS
from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.zone_views import _pin_action

from ..action_links import _CONNECTION_ROUTES
from ..connection_lifecycle import Lifecycle
from ..entity_links import entity_link
from .markup import text


def connect(ref, provider):
    ProviderConnection.objects.create(
        connection_ref=ref,
        controller_id="example-controller",
        provider=provider,
        endpoint="https://service.example.com",
        reachable=True,
        probed=True,
        observed_at=timezone.now(),
    )


class NameTests(TestCase):
    def test_a_connection_is_called_by_the_service_it_reaches(self):
        connect("example-portainer", "portainer")

        link = entity_link("connection", "example-portainer")

        self.assertEqual(link.label, CONNECTION_LABELS["portainer"])
        self.assertEqual(link.title, "example-portainer")

    def test_two_connections_to_one_service_are_told_apart_by_their_names(self):
        connect("example-dns", "cloudflare_dns")
        connect("example-zones", "cloudflare_dns")

        labels = {ref: entity_link("connection", ref).label for ref in ("example-dns", "example-zones")}

        self.assertEqual(len(set(labels.values())), 2)
        for ref, label in labels.items():
            with self.subTest(ref):
                self.assertIn(CONNECTION_LABELS["cloudflare_dns"], label)
                self.assertIn(ref, label)

    def test_a_connection_no_service_claims_keeps_its_own_name(self):
        connect("example-mystery", "")

        self.assertEqual(entity_link("connection", "example-mystery").label, "example-mystery")

    def test_the_connections_page_heads_a_row_by_its_service(self):
        connect("example-portainer", "portainer")
        self.client.force_login(get_user_model().objects.create_superuser("connection-reader"))

        page = text(self.client.get(reverse("control_plane:connections")))

        self.assertIn(CONNECTION_LABELS["portainer"], page)
        self.assertIn(f"Oldest reading {CONNECTION_LABELS['portainer']}", page)


class WordTests(SimpleTestCase):
    def test_no_state_is_said_in_the_word_of_something_to_do(self):
        verbs = {label for _name, label, _field in _CONNECTION_ROUTES}

        self.assertFalse({state.label for state in Lifecycle} & verbs)

    def test_a_state_is_drawn_good_only_while_it_works_and_never_neutral_once_it_needs_a_person(self):
        from ..connection_lifecycle import WORKING
        from ..ui import STATUS_VALUES

        for state in Lifecycle:
            with self.subTest(state):
                self.assertIn(state.tone, STATUS_VALUES)
                self.assertEqual(state.tone == "good", state in WORKING)
                if state.needs_attention:
                    self.assertIn(state.tone, {"attention", "serious"})

    def test_a_domain_is_starred_in_the_word_the_services_list_uses(self):
        for pinned in (True, False):
            with self.subTest(pinned=pinned):
                action = _pin_action(SimpleNamespace(zone="example.com", pinned=pinned))
                self.assertIn("favorite", action.label.lower())
