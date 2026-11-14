"""A browser class's driver leaves the run's stderr as it found it, so a long failure is printed whole."""

import os
import subprocess
import sys

from django.conf import settings
from django.test import SimpleTestCase

# Run with its stderr a pipe, as a gate's workers have: a pool worker whose
# class starts its driver through the gate, and whose one test fails after
# printing more than a pipe holds (64 KiB on Linux), which --buffer writes back
# to stderr in one go as the test stops. The driver stands in for Playwright's:
# a child handed the descriptor Playwright hands Node (`_get_stderr_fileno` in
# playwright/_impl/transport.py: sys.stderr's, else the process's own), which
# makes it non-blocking as Node does and stays up until the driver stops.
WORKER = r"""
import io, multiprocessing, os, subprocess, sys, unittest
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "hq.config.settings")
import django
django.setup()
from hq.platform.core.tests.browser import gate

NODE = "import os, sys; os.set_blocking(2, False); print(flush=True); sys.stdin.read()"


def descriptor_playwright_hands_node():
    try:
        return sys.stderr.fileno()
    except (AttributeError, io.UnsupportedOperation):
        return sys.__stderr__.fileno()


class Driver:
    def start(self):
        self.node = subprocess.Popen(
            [sys.executable, "-c", NODE],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=descriptor_playwright_hands_node(),
        )
        self.node.stdout.readline()
        return self

    def stop(self):
        self.node.stdin.close()
        self.node.wait()


class LongFailure(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = gate.start_driver(cls, Driver)
        cls.addClassCleanup(cls.playwright.stop)

    def test_finds_a_lot(self):
        print("example.invalid finding " * 8000, file=sys.stderr)
        self.fail("example.invalid long failure")


def run(_):
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(LongFailure)
    result = unittest.TextTestRunner(buffer=True, verbosity=0).run(suite)
    return [test.id() for test, _ in result.failures]


if __name__ == "__main__":
    with multiprocessing.get_context("fork").Pool(1) as pool:
        print(pool.map(run, [None])[0])
"""


class DriverOutputTests(SimpleTestCase):
    def test_a_failure_longer_than_the_pipe_holds_is_printed_whole_while_the_driver_runs(self):
        # Buffered, a write the full pipe refuses raises BlockingIOError;
        # unbuffered, as a gate runs its steps, the rest is dropped unsaid.
        for unbuffered in ("", "1"):
            with self.subTest(PYTHONUNBUFFERED=unbuffered):
                done = subprocess.run(
                    [sys.executable, "-c", WORKER],
                    cwd=settings.BASE_DIR,
                    env={**os.environ, "PYTHONUNBUFFERED": unbuffered},
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=300,
                )

                self.assertEqual(done.returncode, 0, done.stderr[-2000:])
                self.assertIn("test_finds_a_lot", done.stdout)
                whole = "example.invalid finding " * 8000 in done.stderr
                self.assertTrue(whole, f"the finding cut short: {len(done.stderr)} characters of stderr")
                self.assertIn("example.invalid long failure", done.stderr)
