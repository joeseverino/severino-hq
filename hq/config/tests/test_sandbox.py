"""A run's directory goes when the run ends, after what multiprocessing made inside it."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from hq.config import sandbox

ROOT = Path(__file__).resolve().parents[3]

# The run's starting process: its directory, and multiprocessing's own inside
# it as a parallel run's forkserver makes one. The base directory is the run's
# whatever its path's length, which otherwise falls back to /tmp past a
# socket's limit.
_RUN = """
import multiprocessing.util
from hq.config import sandbox
multiprocessing.util._get_base_temp_dir = lambda tempfile: tempfile.gettempdir()
run = sandbox._run_directory()
print(run, multiprocessing.util.get_temp_dir(), sep="\\n")
"""


class RunDirectoryTests(SimpleTestCase):
    def test_the_run_s_directory_goes_after_multiprocessing_removed_its_own(self):
        with tempfile.TemporaryDirectory() as base:
            env = {key: value for key, value in os.environ.items() if key != sandbox.RUN} | {"TMPDIR": base}
            ran = subprocess.run(
                [sys.executable, "-c", _RUN], cwd=ROOT, env=env, capture_output=True, text=True, check=False
            )
            run, own = ran.stdout.split()

            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertTrue(Path(own).is_relative_to(run))
            self.assertNotIn("Traceback", ran.stderr)
            self.assertFalse(Path(run).exists())
