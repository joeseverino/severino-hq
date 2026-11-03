"""A container started by an image's tag alone is a finding, with the pin it could name."""

from django.test import TestCase

from ..findings import findings
from ..inventory_testing import store
from .test_docker_estate import NEW, OLD, ON_LAB, READER, estate

PROXY = "example/socket-proxy"
DIGEST = "sha256:" + "e" * 64
OTHER = "sha256:" + "f" * 64


def image(identity: str, reference: str, *, digests=(), service: str = "proxy") -> dict:
    """An image on lab-1 as Portainer reports it, with the one container started from it."""

    return {
        **ON_LAB,
        "id": identity,
        "tags": [f"{PROXY}:1.13.1"],
        "digests": list(digests),
        "containers": [{"container": service, "reference": reference, "service": service}],
    }


class PinTests(TestCase):
    def setUp(self):
        estate()

    def found(self):
        found = findings(principal=READER, rule="container-image-unpinned")["findings"]
        return [finding for finding in found if finding["rule"] == "container-image-unpinned"]

    def test_a_container_pinned_by_digest_raises_nothing(self):
        store("portainer.image", image(NEW, f"{PROXY}:1.13.1@{DIGEST}", digests=[f"{PROXY}@{DIGEST}"]))

        self.assertEqual(self.found(), [])

    def test_a_container_started_by_tag_alone_is_a_finding_naming_its_pin(self):
        store("portainer.image", image(NEW, f"{PROXY}:1.13.1", digests=[f"{PROXY}@{DIGEST}"]))

        (finding,) = self.found()

        pin = f"{PROXY}:1.13.1@{DIGEST}"
        self.assertEqual(finding["subject"], "machine:lab-1")
        self.assertEqual(finding["severity"], "attention")
        self.assertIn({"label": "Pin it as", "value": pin}, finding["evidence"])
        self.assertIn(f"set its image to {pin}", str(finding))
        self.assertIn("docker compose up -d --force-recreate proxy", str(finding))

    def test_the_pin_is_the_digest_of_the_name_it_was_started_by(self):
        # Docker Hub's short name and its full one are the same image.
        store(
            "portainer.image",
            image(
                NEW, "docker.io/example/socket-proxy:1.13.1", digests=[f"example/other@{OTHER}", f"{PROXY}@{DIGEST}"]
            ),
        )

        (finding,) = self.found()

        self.assertIn(
            {"label": "Pin it as", "value": f"docker.io/example/socket-proxy:1.13.1@{DIGEST}"}, finding["evidence"]
        )

    def test_an_image_with_no_registry_digest_to_pin_to_raises_nothing(self):
        # Built on the machine, or pulled under another name: there is nothing to pin to.
        store(
            "portainer.image",
            image(NEW, f"{PROXY}:1.13.1"),
            image(OLD, f"{PROXY}:1.13.1", digests=[f"example/other@{OTHER}"], service="other"),
        )

        self.assertEqual(self.found(), [])
