"""dev:look draws a catalog entry without script, as the catalog's gate judges
it, and a page drawn without script is looked at to the end: with --eval and
--open it never waits on a frame, a timer or a second reading the page never
gives."""

import io
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, tag

from . import look

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
    FIRST_READING = {look._ANSWERED: True}

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
    drawn_with_script = False

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


class _Context:
    """The look's choice of a context to draw each page in, each it starts recorded."""

    drawn_with_script = True

    def __init__(self, unscripted):
        self.unscripted, self.started = frozenset(unscripted), []

    def __getattr__(self, name):
        return getattr(look.Look, name).__get__(self)

    def start(self, **options):
        self.started.append(options["java_script_enabled"])


@tag("source")
class CatalogScriptTests(SimpleTestCase):
    def drawn(self, names, **asked):
        """Whether each of ``names`` was drawn with script, in turn, and the
        contexts started to draw them."""

        context = _Context({"part/example", "table/example"})
        drawn = []
        with mock.patch.dict(os.environ, {**ASKED, **asked}):
            for name in names:
                context.draw_scripted_as(name)
                drawn.append(context.drawn_with_script)
        return drawn, context.started

    def test_a_catalog_entry_is_drawn_without_script_as_its_gate_judges_it_and_a_page_with_script(self):
        drawn, started = self.drawn(["part/example", "table/example", "connections", "part/example"])

        self.assertEqual(drawn, [False, False, True, False])
        # A context is started afresh only where the next page draws otherwise.
        self.assertEqual(started, [False, True, False])

    def test_with_no_script_every_page_is_drawn_without_it(self):
        drawn, _started = self.drawn(["connections", "part/example"], usage_no_script="true")

        self.assertEqual(drawn, [False, False])
