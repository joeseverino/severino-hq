"""The one script that must answer before the page is drawn loads in the head without waiting, and a page's
own script loads on its page alone; file_rules holds every script tag to its nonce and to waiting."""

import re

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from ..file_rules import BEFORE_DRAWING, PAGE_SCRIPTS

SCRIPT = re.compile(r"<script\b[^>]*\bsrc=[^>]*>")


class ScriptLoadingTests(SimpleTestCase):
    def test_the_one_loads_in_the_head_without_waiting(self):
        base = render_to_string("base.html")
        head = base[: base.index("</head>")]
        for name in BEFORE_DRAWING:
            with self.subTest(script=name):
                (found,) = [tag_ for tag_ in SCRIPT.findall(head) if name in tag_]
                self.assertNotRegex(found, r"\b(defer|async)\b")

    def test_a_page_script_loads_on_its_page_alone(self):
        base = render_to_string("base.html")
        for script in PAGE_SCRIPTS:
            with self.subTest(script=script):
                self.assertNotIn(script, base)
