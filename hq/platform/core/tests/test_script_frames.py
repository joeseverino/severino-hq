import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads

# A frame a script clones, named in its selector: template[data-x] or
# template[data-x="value"]. A tone fragment.js computes is NoticeFrameTests'.
_CLONED = re.compile(r"""template\[(data-[\w-]+)(?:=["'](\w+)["'])?\]""")


@tag("source", "tree")
@reads("static/js/**/*.js", "demo/demo.js", "templates/**", "hq/**")
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
                opening = f'<template {name}="{value}"' if value else f"<template {name}"
                self.assertTrue(opening in drawn, f"draw {opening}> in the partial that owns its region")
