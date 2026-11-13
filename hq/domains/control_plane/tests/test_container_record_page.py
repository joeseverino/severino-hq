"""A declared container's record page, whether or not the reading has it running."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.inventory_testing import store

from ..models import ManagedResource

ON_BOX = {"host": "example-box", "host_address": "192.0.2.10", "connection_ref": "example-portainer"}


def running(name: str) -> dict:
    return {
        **ON_BOX, "name": name, "stack": name, "image": "example/web:1.0.0", "state": "running",
        "status": "Up 2 days (healthy)", "ports": [], "network_mode": "bridge", "portainer_managed": False, "id": "",
    }  # fmt: skip


class ContainerRecordPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("operator", password="x" * 20))
        ManagedResource.objects.create(
            key="example-box", kind="machine", spec={"name": "example-box", "addresses": ["192.0.2.10"]}
        )
        for name in ("web", "gone"):
            ManagedResource.objects.create(
                key=f"example-box-{name}",
                kind="portainer.container",
                spec={"host": "example-box", "name": name, "connection_ref": "example-portainer"},
            )
        # The sweep reads the machine and finds only one of the two declared.
        store("portainer.container", running("web"))

    def open(self, name: str):
        return self.client.get(reverse("control_plane:detail", args=[f"example-box-{name}"]))

    def test_a_container_the_reading_lacks_still_has_its_page(self):
        response = self.open("gone")

        self.assertEqual(response.status_code, 200)
        # Nothing runs, so the page has no running band and keeps the record's status beside it.
        self.assertIsNone(response.context["container"]["standing"])
        self.assertEqual(response.context["page_aside"], "control_plane/_record_status.html")

    def test_a_running_container_is_summarized_by_what_it_runs(self):
        response = self.open("web")

        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["container"]["standing"])
