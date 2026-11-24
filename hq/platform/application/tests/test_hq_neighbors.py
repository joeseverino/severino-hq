"""A problem about another container on HQ's machine is that container's, never HQ's."""

from django.test import TestCase
from django.utils import timezone

from hq.platform.application.security import cli_principal

from ..findings import estate_findings
from ..hq_context import hq_context
from ..inventory_testing import store
from ..projection import projection_scope
from .test_hq_context import IMAGE, estate, own, running
from .test_tailnet_posture import EVERYTHING

OTHER = "example/other:1.0.0"


def unpinned(*names: str) -> None:
    """Containers on HQ's machine started by tag from an image a registry holds."""

    store("portainer.container", running("app", IMAGE, "0123456789ab"), *(running(name, OTHER) for name in names))
    store(
        "portainer.image",
        {
            "connection_ref": "example-portainer", "host": "example-box", "id": "sha256:" + "e" * 64,
            "tags": [OTHER], "digests": [f"example/other@sha256:{'f' * 64}"], "size": 10,
            "containers": [{"container": name, "reference": OTHER} for name in names],
        },
    )  # fmt: skip
    store(
        "registry.image", {"image": "docker.io/example/other", "tags": ["1.0.0"], "read_at": timezone.now().isoformat()}
    )


class NeighborTests(TestCase):
    def test_a_neighbors_problem_is_not_counted_as_hqs(self):
        estate()
        unpinned("example-neighbor")
        first, second = own("192.0.2.44")
        with first, second:
            found = hq_context(principal=cli_principal())

        self.assertEqual([item["label"] for item in found.problems], [])
        self.assertEqual(found.status, ("good", "Running"))


class KeyTests(TestCase):
    def test_two_containers_on_one_unpinned_image_are_one_item_naming_both(self):
        estate()
        unpinned("example-one", "example-two")

        with projection_scope():
            found = [
                finding
                for finding in estate_findings(principal=EVERYTHING)
                if finding.rule == "container-image-unpinned"
            ]

        (finding,) = found
        self.assertEqual(
            [str(value) for label, value in finding.evidence if label == "Container"], ["example-one", "example-two"]
        )
