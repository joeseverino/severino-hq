"""Docker readings in the relation graph, on the machine page, and as findings."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from control_plane.models import ManagedResource, ProviderInventory

from ..docker_estate import IMAGE_BEHIND, IMAGE_UNTAGGED, image_verdicts, reference_tag
from ..findings import findings
from ..inventory_testing import store
from ..projection import projection_scope
from ..relationships import relationships_for
from ..security import Capability, Principal
from ..topology import relation_graph
from ..topology_model import RELATIONS

READER = Principal("reader", "test", frozenset({Capability.READ}))
ON_LAB = {"connection_ref": "example-portainer", "environment_id": 1, "host": "lab-1",
          "host_address": "192.0.2.10"}
OLD, NEW, LOOSE = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64


def images():
    return (
        {**ON_LAB, "id": OLD, "tags": [],
         "containers": [{"container": "web", "reference": "example/web:1", "service": "web"}]},
        {**ON_LAB, "id": NEW, "tags": ["example/web:1"], "containers": []},
        {**ON_LAB, "id": LOOSE, "tags": [],
         "containers": [{"container": "job", "reference": LOOSE}]},
    )


def estate():
    ManagedResource.objects.create(
        key="lab-1", kind="machine", spec={"name": "lab-1", "addresses": ["192.0.2.10"]}
    )
    for name in ("web", "db", "cache"):
        ManagedResource.objects.create(
            key=f"lab-{name}", kind="portainer.container",
            spec={"host": "lab-1", "name": name, "connection_ref": "example-portainer"},
        )
    store(
        "portainer.network",
        {**ON_LAB, "name": "shop_default", "driver": "bridge", "subnets": ["172.18.0.0/16"],
         "containers": ["db", "web"]},
        {**ON_LAB, "name": "bridge", "driver": "bridge", "containers": ["cache", "web"]},
    )
    store(
        "portainer.volume",
        {**ON_LAB, "type": "volume", "name": "shop_data", "source": "/var/lib/docker/volumes/shop_data",
         "stack": "shop", "used_by": [{"container": "db", "destination": "/data"}]},
        {**ON_LAB, "type": "bind", "source": "/opt/apps/shop/config",
         "used_by": [{"container": "web", "destination": "/config", "read_only": True}]},
    )
    store("portainer.image", *images())
    store(
        "portainer.environment",
        {"connection_ref": "example-portainer", "id": 1, "name": "local", "host": "lab-1",
         "address": "192.0.2.10", "type": "docker", "status": "up", "docker_version": "27.1.1",
         "containers_running": 2, "containers_total": 3},
    )
    store(
        "portainer.compose_project",
        {**ON_LAB, "name": "shop", "source": "compose", "working_dir": "/opt/apps/shop",
         "containers": ["db", "web"]},
    )


class VerdictTests(SimpleTestCase):
    def test_a_reference_without_a_tag_is_latest_and_an_id_is_none(self):
        self.assertEqual(reference_tag("example/db"), "example/db:latest")
        self.assertEqual(reference_tag("registry.example.com:5000/app"),
                         "registry.example.com:5000/app:latest")
        self.assertEqual(reference_tag("example/web:1"), "example/web:1")
        self.assertEqual(reference_tag(LOOSE), "")
        self.assertEqual(reference_tag("example/web@sha256:" + "d" * 64), "")

    def test_an_image_the_tag_moved_off_is_behind_and_one_without_tags_untagged(self):
        records = (
            {"id": OLD, "tags": ["example/web:0"],
             "containers": [{"container": "web", "reference": "example/web:1"}]},
            {"id": NEW, "tags": ["example/web:1"],
             "containers": [{"container": "fresh", "reference": "example/web:1"}]},
            {"id": LOOSE, "tags": [], "containers": [{"container": "job", "reference": LOOSE}]},
        )

        found = {(fact, container) for fact, container, *_rest in image_verdicts(records)}

        self.assertEqual(found, {(IMAGE_BEHIND, "web"), (IMAGE_UNTAGGED, "job")})

    def test_a_container_on_the_image_its_tag_names_is_current(self):
        records = ({"id": NEW, "tags": ["example/db:latest"],
                    "containers": [{"container": "db", "reference": "example/db"}]},)

        self.assertEqual(image_verdicts(records), [])


class GraphTests(TestCase):
    def setUp(self):
        estate()

    def test_containers_on_a_shared_network_talk_and_the_default_bridge_says_nothing(self):
        with projection_scope():
            edges = relation_graph(principal=READER).topology.edges

        talks = {(edge.source, edge.target, edge.detail) for edge in edges if edge.kind == "talks_to"}
        self.assertEqual(talks, {("resource:lab-db", "resource:lab-web", "Network shop_default")})

    def test_the_machine_holds_each_reading_through_the_portainer_connection(self):
        with projection_scope():
            found = relationships_for("machine:lab-1", principal=READER)

        self.assertEqual(found.labels("Docker network"), ("bridge", "shop_default"))
        self.assertIn("shop_data", found.labels("Holds data in"))
        self.assertIn("/opt/apps/shop/config", found.labels("Holds data in"))
        self.assertEqual(found.labels("Docker environment"), ("local",))
        self.assertEqual(found.labels("Compose project"), ("shop",))

    def test_a_declared_container_says_what_it_talks_to(self):
        with projection_scope():
            found = relationships_for("resource:lab-web", principal=READER)

        # Named as the container: its machine is the context, not its name.
        self.assertEqual(found.labels(RELATIONS["talks_to"].inverse), ("db",))


class FindingTests(TestCase):
    def setUp(self):
        estate()

    def found(self, rule):
        return findings(principal=READER, rule=rule)["findings"]

    def test_a_container_behind_its_tag_is_a_finding_with_the_recreate_step(self):
        (finding,) = self.found("container-image-behind")

        self.assertEqual(finding["subject"], "machine:lab-1")
        self.assertEqual(finding["title"], "web on lab-1 runs an older example/web:1")
        self.assertIn({"label": "Tagged now", "value": "b" * 12}, finding["evidence"])
        self.assertIn("docker compose up -d --force-recreate web", str(finding))

    def test_an_untagged_image_is_a_finding(self):
        titles = [finding["title"] for finding in self.found("container-image-untagged")]

        self.assertEqual(titles, ["job on lab-1 runs an untagged image"])

    def test_nothing_is_claimed_when_images_are_current(self):
        store("portainer.image", {**ON_LAB, "id": NEW, "tags": ["example/web:1"],
                                  "containers": [{"container": "web", "reference": "example/web:1"}]})

        self.assertEqual(self.found("container-image-behind"), [])
        self.assertEqual(self.found("container-image-untagged"), [])


class MachinePageTests(TestCase):
    def setUp(self):
        estate()
        self.client.force_login(
            get_user_model().objects.create_superuser("operator", password="x" * 20)
        )

    def test_the_machine_page_says_where_data_lives_and_what_runs_behind(self):
        response = self.client.get(reverse("control_plane:machine", args=["lab-1"]))

        self.assertEqual(response.status_code, 200)
        for text in ("Docker environment", "Where data lives", "/opt/apps/shop/config",
                     "Networks", "172.18.0.0/16", "Compose projects", "/opt/apps/shop",
                     "Behind its tag", "Untagged", "27.1.1"):
            self.assertContains(response, text)

    def test_a_machine_nothing_reads_docker_for_has_no_docker_bands(self):
        ProviderInventory.objects.filter(kind__startswith="portainer.").delete()

        response = self.client.get(reverse("control_plane:machine", args=["lab-1"]))

        self.assertNotContains(response, "Where data lives")
        self.assertNotContains(response, 'id="docker-images"')

    def refuse_edge(self):
        store(
            "portainer.image",
            *images(),
            refused_parts=[{"part": "", "refusal": "permission", "reason": "denied",
                            "scope": "edge-2", "address": "198.51.100.30",
                            "connection_ref": "example-portainer"}],
        )

    def test_one_environment_refused_is_said_on_that_machine_only(self):
        ManagedResource.objects.create(
            key="edge-2", kind="machine", spec={"name": "edge-2", "addresses": ["198.51.100.30"]}
        )
        self.refuse_edge()

        edge = self.client.get(reverse("control_plane:machine", args=["edge-2"]))
        lab = self.client.get(reverse("control_plane:machine", args=["lab-1"]))

        self.assertRegex(edge.content.decode(), r"Not readable: [^<]*Docker image")
        self.assertNotRegex(lab.content.decode(), r"Not readable: [^<]*Docker image")
        self.assertContains(lab, "Behind its tag")

    def test_a_machine_known_by_its_address_alone_still_hears_it(self):
        from ..facts import Subject, refusals_about

        self.refuse_edge()

        (refused,) = refusals_about(Subject.of(hostnames=("vps",), addresses=("198.51.100.30",)))
        self.assertEqual(refused.phrase, "Docker image not read: missing environment access")
        self.assertEqual(refusals_about(Subject.of(hostnames=("lab-1",), addresses=("192.0.2.10",))),
                         ())
