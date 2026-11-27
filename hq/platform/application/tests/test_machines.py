"""Machines, and where each one's facts come from.

Observation first: a machine is here because something reported it (a
credential that reaches it, a container running on it, a service served from it)
so adding a VPS is registering it somewhere rather than entering it here.

A declaration is the other half, for what nothing can sweep: a printer, an
offline CA, a phone. It carries what the machine is for and the addresses that
reach it, and it puts the machine on the board by itself.
"""

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ProviderInventory
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.testing import connection, declare, operator

from ..capabilities import capability_words
from ..machines import machine
from ..resource_capabilities import UPDATE_CAPABILITY
from ..services import CONTAINER_KIND


def containers(*records):
    ProviderInventory.objects.update_or_create(
        kind=CONTAINER_KIND,
        defaults={
            "records": list(records),
            "reachable": True,
            "observed_at": timezone.now(),
        },
    )


class MachinePageTests(TestCase):
    def setUp(self):
        operator(self.client)
        connection(
            "portainer",
            "a-portainer",
            endpoint="https://portainer.example",
            reaches=["a-docker-host"],
        )
        containers(
            {
                "name": "app",
                "host": "a-docker-host",
                "image": "nginx:alpine",
                "state": "running",
                "ports": [8081],
            }
        )

    def test_the_page_never_carries_a_credential(self):
        response = self.client.get(reverse("control_plane:machine", kwargs={"name": "a-docker-host"}))

        self.assertNotContains(response, "API_TOKEN")
        self.assertNotContains(response, "PASSWORD")


class WhoeverSweptTests(TestCase):
    """The name a sweep files containers under is not always a machine's name.

    Portainer calls its own environment "local", and a controller filling that
    in has nothing to offer but its own hostname. Run the sweep from a laptop
    and every container on the Docker host is reported as running on the
    laptop: a machine that has never run any of them.
    """

    def setUp(self):
        declare("a-docker-host", "machine", {"name": "a-docker-host", "role": "Docker host", "addresses": ["10.0.0.9"]})
        containers(
            {
                "name": "a-service",
                "host": "a-laptop-that-swept",
                "host_address": "10.0.0.9",
                "state": "running",
                "connection_ref": "a-portainer",
            }
        )

    def test_another_name_finds_the_machine_and_its_page_redirects_there(self):
        self.assertEqual(machine("a-laptop-that-swept").name, "a-docker-host")
        operator(self.client, "op")

        response = self.client.get(reverse("control_plane:machine", kwargs={"name": "a-laptop-that-swept"}))

        self.assertRedirects(
            response,
            reverse("control_plane:machine", kwargs={"name": "a-docker-host"}),
            fetch_redirect_response=False,
        )


class IdentifierIsFixedTests(TestCase):
    """The identifier is filing, and a form should not ask about filing. Shown
    as an input beneath a field called "Name", the resource appears to have two
    names, and the fixed one looks like the optional one.
    """

    def setUp(self):
        self.resource = declare("a-box", "machine", {"name": "a-box", "addresses": []})
        operator(self.client, "op")

    def test_a_save_keeps_the_identifier_it_already_had(self):
        response = self.client.post(
            reverse("control_plane:edit", kwargs={"key": "a-box"}),
            form_keyed({"name": "a-box", "role": "A renamed purpose", "enabled": "on"}),
        )

        self.assertEqual(response.status_code, 302)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.key, "a-box")
        self.assertEqual(self.resource.spec["role"], "A renamed purpose")


class DeclareFromWhatHQKnowsTests(TestCase):
    """ "Add this machine to HQ" opens a form holding the name and addresses HQ has."""

    def setUp(self):
        operator(self.client)
        ProviderInventory.objects.update_or_create(
            kind="tailscale.device",
            defaults={
                "records": [
                    {
                        "name": "example-host",
                        "online": True,
                        "addresses": ["100.64.0.7"],
                    }
                ],
                "observed_at": timezone.now(),
            },
        )

    def test_the_form_opens_holding_the_seed(self):
        from urllib.parse import urlencode

        response = self.client.get(
            reverse("control_plane:create")
            + "?"
            + urlencode(
                {"kind": "machine", "name": "example-host", "addresses": ["100.64.0.7", "192.0.2.7"]},
                doseq=True,
            )
        )

        form = response.context["spec"]
        self.assertEqual(form.initial["name"], "example-host")
        self.assertEqual(form.initial["addresses"], ["100.64.0.7", "192.0.2.7"])

    def test_saving_it_returns_to_the_machine_it_now_declares(self):
        from urllib.parse import urlencode

        query = urlencode(
            {
                "kind": "machine",
                "next": reverse("control_plane:machine", kwargs={"name": "example-host"}),
            }
        )
        response = self.client.post(
            reverse("control_plane:create") + "?" + query,
            form_keyed({"name": "example-host", "addresses": ["100.64.0.7"]}),
        )

        self.assertRedirects(
            response,
            reverse("control_plane:machine", kwargs={"name": "example-host"}),
            fetch_redirect_response=False,
        )
        self.assertEqual(machine("example-host").declaration, "example-host")
        page = self.client.get(reverse("control_plane:machine", kwargs={"name": "example-host"}))
        labels = [action.label for action in page.context["page"].actions]
        self.assertIn(capability_words(UPDATE_CAPABILITY), labels)
