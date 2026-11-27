"""Docker readings on the machine page."""

from django.test import TestCase
from django.urls import reverse

from hq.platform.core.testing import declare, operator

from ..inventory_testing import store

ON_LAB = {"connection_ref": "example-portainer", "environment_id": 1, "host": "lab-1", "host_address": "192.0.2.10"}
OLD, NEW, LOOSE = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64


def images():
    return (
        {
            **ON_LAB,
            "id": OLD,
            "tags": [],
            "containers": [{"container": "web", "reference": "example/web:1", "service": "web"}],
        },
        {**ON_LAB, "id": NEW, "tags": ["example/web:1"], "containers": []},
        {**ON_LAB, "id": LOOSE, "tags": [], "containers": [{"container": "job", "reference": LOOSE}]},
    )


def estate():
    declare("lab-1", "machine", {"name": "lab-1", "addresses": ["192.0.2.10"]})
    declare("lab-web", "portainer.container", {"host": "lab-1", "name": "web", "connection_ref": "example-portainer"})
    store("portainer.image", *images())
    store("portainer.network", {**ON_LAB, "name": "bridge", "driver": "bridge", "containers": ["cache"]})


class MachinePageTests(TestCase):
    def setUp(self):
        estate()
        operator(self.client)

    def refuse_edge(self):
        store(
            "portainer.image",
            *images(),
            refused_parts=[
                {
                    "part": "",
                    "refusal": "permission",
                    "reason": "denied",
                    "scope": "edge-2",
                    "address": "198.51.100.30",
                    "connection_ref": "example-portainer",
                }
            ],
        )

    def test_one_environment_refused_is_said_on_that_machine_only(self):
        declare("edge-2", "machine", {"name": "edge-2", "addresses": ["198.51.100.30"]})
        self.refuse_edge()

        edge = self.client.get(reverse("control_plane:machine", args=["edge-2"]))
        lab = self.client.get(reverse("control_plane:machine", args=["lab-1"]))

        self.assertRegex(edge.content.decode(), r"Could not be read: [^<]*Docker image")
        self.assertNotRegex(lab.content.decode(), r"Could not be read: [^<]*Docker image")
        self.assertContains(lab, "Newer build pulled")
