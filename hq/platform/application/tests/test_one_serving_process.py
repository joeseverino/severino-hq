"""The image serves from one process, which the agent limits rely on: they are counted in its memory."""

import json
import re
from pathlib import Path

from django.test import SimpleTestCase, tag

ROOT = Path(__file__).resolve().parents[4]


def _command() -> list[str]:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    found = re.search(r"^CMD (\[.*?\])\s*$", dockerfile.replace("\\\n", ""), re.MULTILINE)
    assert found, "the Dockerfile has no exec-form CMD"
    return json.loads(found[1])


@tag("source")
class OneServingProcessTests(SimpleTestCase):
    def test_the_image_names_one_uvicorn_process(self):
        # Named, never left to the default: uvicorn reads WEB_CONCURRENCY from
        # the environment only when --workers is absent.
        command = _command()

        self.assertEqual(command[0], "uvicorn")
        workers = [part for part in command if part.startswith("--workers")]
        self.assertIn(workers, (["--workers"], ["--workers=1"]))
        if workers == ["--workers"]:
            self.assertEqual(command[command.index("--workers") + 1], "1")
