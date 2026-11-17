"""An image a registry holds, started by its tag alone, is a finding, with the pin it could name."""

from django.test import TestCase
from django.utils import timezone

from ..findings import findings
from ..inventory_testing import store
from .test_containers import running
from .test_image_findings import READER

PROXY = "example/socket-proxy"
DIGEST = "sha256:" + "e" * 64
OTHER = "sha256:" + "f" * 64


def started(reference: str, *, digests=()) -> None:
    """One container on example-box started from ``reference``, its image as
    Portainer reports it, and the registry read for that image."""

    store("portainer.container", running("proxy", reference))
    store(
        "portainer.image",
        {
            "connection_ref": "example-portainer",
            "host": "example-box",
            "id": DIGEST,
            "tags": [f"{PROXY}:1.13.1"],
            "digests": list(digests),
            "containers": [{"container": "proxy", "reference": reference, "service": "proxy"}],
        },
    )
    store("registry.image", {"image": f"docker.io/{PROXY}", "tags": ["1.13.1"], "read_at": timezone.now().isoformat()})


class PinTests(TestCase):
    def found(self):
        found = findings(principal=READER, rule="container-image-unpinned")["findings"]
        return [finding for finding in found if finding["rule"] == "container-image-unpinned"]

    def test_a_container_pinned_by_digest_raises_nothing(self):
        started(f"{PROXY}:1.13.1@{DIGEST}", digests=[f"{PROXY}@{DIGEST}"])

        self.assertEqual(self.found(), [])

    def test_a_container_started_by_tag_alone_is_a_finding_naming_its_pin(self):
        started(f"{PROXY}:1.13.1", digests=[f"{PROXY}@{DIGEST}"])

        (finding,) = self.found()

        pin = f"{PROXY}:1.13.1@{DIGEST}"
        self.assertEqual(finding["subject"], "machine:example-box")
        self.assertEqual(finding["severity"], "attention")
        self.assertIn({"label": "Pin it as", "value": pin}, finding["evidence"])
        self.assertIn(f"set the image of proxy to {pin}", str(finding))

    def test_the_pin_is_the_digest_of_the_name_it_was_started_by(self):
        # Docker Hub's short name and its full one are the same image.
        started(f"docker.io/{PROXY}:1.13.1", digests=[f"example/other@{OTHER}", f"{PROXY}@{DIGEST}"])

        (finding,) = self.found()

        self.assertIn({"label": "Pin it as", "value": f"docker.io/{PROXY}:1.13.1@{DIGEST}"}, finding["evidence"])

    def test_an_image_with_no_registry_digest_to_pin_to_raises_nothing(self):
        # Pulled under another name: there is nothing to pin to.
        started(f"{PROXY}:1.13.1", digests=[f"example/other@{OTHER}"])

        self.assertEqual(self.found(), [])
