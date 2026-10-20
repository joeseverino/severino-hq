"""Every script waits for the page, except the one that must answer before it is drawn."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

from .source import source_files

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = re.compile(r"<script\b[^>]*\bsrc=[^>]*>")
# `pagereveal` fires before the new page's first frame, which is before a
# deferred script runs, so its listener loads in the head without `defer`.
BEFORE_DRAWING = "js/transitions.js"


@tag("source")
class ScriptLoadingTests(SimpleTestCase):
    def test_every_script_is_deferred_but_the_one_answering_the_page_transition(self):
        blocking = [
            f"{path.relative_to(ROOT)}: {tag_}"
            for path in source_files(ROOT / "templates", "*.html")
            for tag_ in SCRIPT.findall(path.read_text(encoding="utf-8"))
            if not re.search(r"\bdefer\b|type=\"module\"", tag_) and BEFORE_DRAWING not in tag_
        ]
        self.assertEqual(blocking, [])

    def test_the_page_transition_is_answered_only_where_it_loads_in_time(self):
        listeners = [
            path.name
            for path in source_files(ROOT / "static" / "js", "*.js")
            if re.search(r"[\"']page(reveal|swap)[\"']", path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(listeners, [Path(BEFORE_DRAWING).name])
