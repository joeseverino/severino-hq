import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import RequestFactory, SimpleTestCase, tag

from hq.platform.application.tests.source import template_files

# A frame a script clones, named in its selector: template[data-x] or
# template[data-x="value"]. A tone fragment.js computes is NoticeFrameTests'.
_CLONED = re.compile(r"""template\[(data-[\w-]+)(?:=["'](\w+)["'])?\]""")
# One a region holds, found from that region's element rather than the page:
# `zone.querySelector("template[...")`.
_SCOPED = re.compile(r"""\b(?!document\b)\w+\.querySelector\(\s*["'`]$""")
# A notice of a tone fragment.js names, cloned through its notice("<tone>").
_TONE = re.compile(r"""\bnotice\("(\w+)"\)""")


def _opening(name, value):
    return f'<template {name}="{value}"' if value else f"<template {name}"


def _page_frames(script):
    """The frames ``script`` clones from the page, wherever on it they are."""

    text = script.read_text(encoding="utf-8")
    found = {
        match.groups()
        for match in _CLONED.finditer(text)
        if not _SCOPED.search(text[max(0, match.start() - 80) : match.start()])
    }
    return found | {("data-notice", tone) for tone in _TONE.findall(text)}


def _part(name):
    return f"scripts/{name}.html"


@tag("source", "tree")
class ScriptFrameTests(SimpleTestCase):
    def test_every_frame_a_script_clones_is_drawn_by_the_server(self):
        """A script adds markup only by cloning a <template> the server drew,
        so each one it names is drawn by a template or a widget."""

        root = Path(settings.BASE_DIR)
        scripts = [*sorted((root / "static" / "js").rglob("*.js")), root / "demo" / "demo.js"]
        cloned = {found for path in scripts for found in _CLONED.findall(path.read_text(encoding="utf-8"))}
        sources = [
            *root.glob("templates/**/*.html"),
            *root.glob("hq/**/templates/**/*.html"),
            *(path for path in root.glob("hq/**/*.py") if "tests" not in path.parts),
        ]
        drawn = "\n".join(path.read_text(encoding="utf-8") for path in sorted(sources))

        self.assertTrue(cloned, "the scripts clone no frame: the pattern no longer reads them")
        for name, value in sorted(cloned):
            with self.subTest(frame=name, value=value):
                opening = _opening(name, value)
                self.assertTrue(opening in drawn, f"draw {opening}> in the partial that owns its region")

    def test_a_script_cloning_from_the_page_loads_only_with_its_frames(self):
        """A script that clones a frame from anywhere on the page loads only
        through its part, scripts/<name>.html, which draws each such frame
        beside its tag. So no page, a test's probe included, has the script
        without its frames, and base.html draws each frame once."""

        root = Path(settings.BASE_DIR)
        cloning = {
            path.stem: frames
            for path in sorted((root / "static" / "js").glob("*.js"))
            if (frames := _page_frames(path))
        }
        loaders = sorted(
            {
                *(path for _, path in template_files()),
                *root.glob("hq/**/*.html"),
                *root.glob("hq/**/*.py"),
                *root.glob("demo/**/*.py"),
            }
        )
        request = RequestFactory().get("/")
        request.user = None
        page = render_to_string("base.html", {}, request=request)

        self.assertIn("fragment", cloning, "fragment.js clones no frame: the patterns no longer read it")
        for name, frames in sorted(cloning.items()):
            with self.subTest(script=name):
                tag_ = re.compile(rf"<script\b[^>]*\bsrc=[^>]*\bjs/{re.escape(name)}\.js\b")
                part = render_to_string(_part(name), {"csp_nonce": "example"})
                self.assertRegex(part, tag_)
                for frame in sorted(frames):
                    self.assertIn(_opening(*frame), part, f"draw it in {_part(name)}")
                    self.assertEqual(page.count(_opening(*frame)), 1, f"{_opening(*frame)}> on every page, once")
                elsewhere = [
                    str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
                    for path in loaders
                    if tag_.search(path.read_text(encoding="utf-8")) and path != root / "templates" / _part(name)
                ]
                self.assertEqual(elsewhere, [], f'load it with {{% include "{_part(name)}" %}}')
