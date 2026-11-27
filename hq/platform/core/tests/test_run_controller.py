"""The web container's listener: the bridge socket is served beside it, never by it."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

DOCKERFILE = Path(__file__).resolve().parents[4] / "Dockerfile"


# The image is built without its Dockerfile, so this runs from a checkout.
@tag("source")
class WebListenerTests(SimpleTestCase):
    def test_the_web_listener_is_not_the_socket(self):
        command = re.search(r"^CMD \[(.+?)\]$", DOCKERFILE.read_text(), re.MULTILINE | re.DOTALL).group(1)
        self.assertIn('"hq.config.serve"', command)
        self.assertNotIn("--uds", command)
        self.assertNotIn("--fd", command)
        self.assertNotIn("bridge", command)
