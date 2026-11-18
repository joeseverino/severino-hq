"""dev:look draws a page without script to the end: with --eval and --open
it never waits on a frame, a timer or a second reading the page never
gives."""

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

from . import look
from .browser import survey

ASKED = {"usage_pages": "part/example"}


class _Hung(Exception):
    """What the fake page raises where a real one, drawn without script,
    would leave the look waiting for good."""


class _Unscripted:
    """A page drawn without script, as a browser runs one: a script dev:look
    sends runs, but nothing the page schedules does, so a frame callback or a
    timer never fires and a poll reads the page once and never again."""

    # What a predicate the look polls reads at its one reading: --open's
    # regions have all answered, since without script none is loading.
    FIRST_READING = {survey.ANSWERED: True}

    def evaluate(self, script, _arg=None):
        if "requestAnimationFrame" in script or "setTimeout" in script:
            raise _Hung(f"waited on a frame or timer a page without script never runs: {script[:60]}")
        if script == look._RETURNED:
            return "Example"
        # Anything else read once, an extent among them.
        return [820, 1200]

    def evaluate_handle(self, _script):
        return mock.Mock()

    def wait_for_function(self, script, arg=None, polling="raf"):
        if self.FIRST_READING.get(script):
            return mock.Mock()
        raise _Hung(f"polled again a page without script, which reads once: {script[:60]}")


class _Looker:
    """The look at one page drawn without script, its shots kept as the
    bands they would be cut into."""

    css, script, dialogs, on_reference = "", "() => document.title", [], False
    shooting = True
    probe = probe_source = ""
    probe_arg = None
    runs_script = False

    def __init__(self, page):
        self.page, self.measured, self.base, self.shots = page, {}, None, []
        self.opened = 0

    def __getattr__(self, name):
        return getattr(look.Look, name).__get__(self)

    def refused(self, _name, _width):
        return ""

    def as_a_reader(self, _script, _arg=None):
        # One round opens what is closed; the next finds nothing left.
        self.opened += 1
        return self.opened == 1

    def in_bands(self, shot, extent, _shows):
        self.shots.append((shot.name, extent))
        return [(shot.name, 0, extent[1])]


@tag("source")
class UnscriptedLookTests(SimpleTestCase):
    def test_a_page_drawn_without_script_is_looked_at_to_the_end_with_eval_and_open(self):
        asked = {**ASKED, "usage_open": "true", "usage_no_script": "true"}
        with (
            tempfile.TemporaryDirectory() as out,
            mock.patch.object(look, "this_run", lambda: Path(out)),
            mock.patch.dict(os.environ, asked),
            mock.patch("sys.stdout", new_callable=io.StringIO),
        ):
            looker = _Looker(_Unscripted())
            said = looker.look("part/example", 820, "")
            kept = (Path(out) / "part-example-820.eval.txt").read_text(encoding="utf-8")
        self.assertEqual(said, [])
        self.assertEqual(looker.shots, [("part-example-820.png", (820, 1200))])
        self.assertIn("Example", kept)
