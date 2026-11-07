"""Where HQ says it listens is where its image serves."""

import itertools
import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from ..hq_self import LISTENS_AT


@tag("source")
class ListensAtTests(SimpleTestCase):
    def test_it_is_the_address_and_port_the_images_command_gives_the_server(self):
        dockerfile = (Path(settings.BASE_DIR) / "Dockerfile").read_text()
        command = re.search(r"^CMD \[(.*?)\]\s*$", dockerfile, re.MULTILINE | re.DOTALL)
        self.assertIsNotNone(command)
        words = re.findall(r'"([^"]*)"', command.group(1))
        given = dict(itertools.pairwise(words))

        self.assertEqual((given["--host"], int(given["--port"])), LISTENS_AT)
