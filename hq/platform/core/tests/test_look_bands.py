"""dev:look cuts every whole-page shot taller than the window into window-sized bands."""

import ast
import tempfile
from pathlib import Path
from typing import ClassVar
from unittest import mock

from django.test import SimpleTestCase, tag

from . import look


@tag("source")
class BandTests(SimpleTestCase):
    def test_a_tall_page_is_cut_into_window_sized_bands_that_cover_it_once(self):
        bands = look._bands(2000, 900)
        self.assertEqual(bands, [(0, 900), (900, 900), (1800, 200)])
        self.assertEqual(sum(height for _, height in bands), 2000)

    def test_a_page_the_window_holds_is_not_cut(self):
        self.assertEqual(look._bands(900, 900), [])
        self.assertEqual(look._bands(400, 900), [])

    def shot(self, take):
        """The files ``take`` shot of a page 2000 px tall in a window 900 tall
        whose height never holds still, each as its name, top and height."""

        class Moved(Exception):
            pass

        class Page:
            viewport_size = {"width": 1360, "height": 900}
            clips: ClassVar[list[tuple[str, int, int]]] = []

            def wait_for_function(self, *_args, **_options):
                raise Moved("Timeout 5000ms exceeded.")

            def evaluate(self, *_args):
                return [1360, 2000]

            def screenshot(self, path, clip, **_options):
                self.clips.append((Path(path).name, clip["y"], clip["height"]))

        # The look's shooting, whichever of its methods it shoots through.
        shooting = {name: member for name, member in vars(look.Look).items() if callable(member)}
        looker = type("Looker", (), {**shooting, "page": Page(), "shots": []})()

        with (
            tempfile.TemporaryDirectory() as out,
            mock.patch.object(look, "this_run", lambda: Path(out)),
            mock.patch.object(look, "BrowserError", Moved),
        ):
            take(looker)
        return Page.clips

    def test_a_page_whose_height_never_holds_still_is_cut_in_bands_all_the_same(self):
        clips = self.shot(lambda looker: looker.screenshot("estate/hq", 1360))
        self.assertEqual(clips[0], ("estate-hq-1360.png", 0, 2000))
        bands = [(f"estate-hq-1360.band-0{n}.png", *band) for n, band in enumerate(look._bands(2000, 900), start=1)]
        self.assertEqual(clips[1:], bands)

    def test_every_whole_page_shot_is_taken_where_it_is_cut_in_bands(self):
        tree = ast.parse(Path(look.__file__).read_text(encoding="utf-8"))
        shooting = {
            function.name
            for function in ast.walk(tree)
            if isinstance(function, ast.FunctionDef)
            for call in ast.walk(function)
            if isinstance(call, ast.Call) and getattr(call.func, "id", "") == "shoot_page"
        }
        self.assertEqual(shooting, {"in_bands"})

    def test_a_page_shot_as_it_failed_is_cut_in_bands(self):
        clips = self.shot(lambda looker: looker.shoot_failure("estate/hq", 1360, "survey could not run"))
        bands = [f"estate-hq-1360.failed.band-0{n}.png" for n in (1, 2, 3)]
        self.assertEqual([name for name, _, _ in clips], ["estate-hq-1360.failed.png", *bands])
