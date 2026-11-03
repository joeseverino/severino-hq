"""Every template's code blocks and disclosures are in a keyboard's reach.

A ``pre`` keeps its lines and scrolls sideways in its own box rather than
widen the page, and a box that scrolls with nothing in it to focus is out of
a keyboard's reach (WCAG 2.1.1), so each takes focus itself. A disclosure's
summary is the control that opens it, and a control inside it is one a
screen reader announces as part of that toggle and a press may reach in
place of the toggle, so what it leads to sits in the body.
"""

import re

from django.test import SimpleTestCase, tag

from .source import template_files

_PRE = re.compile(r"<pre(?![\w-])[^>]*>")
_SUMMARY = re.compile(r"<summary\b[^>]*>(.*?)</summary>", re.DOTALL)
# What draws a control: the element itself, or a tag that renders a link or a button.
_CONTROL = re.compile(
    r"<(?:a|button|input|select|textarea)\b|\btabindex=|\{%\s*(?:entity|post_button|cell_line|page_action)\b"
)


@tag("source", "tree")
class KeyboardReachTests(SimpleTestCase):
    def test_every_code_block_takes_focus(self):
        unreachable = [
            f"{name}: {opening}"
            for name, path in template_files()
            for opening in _PRE.findall(path.read_text(encoding="utf-8"))
            if 'tabindex="0"' not in opening
        ]
        self.assertEqual(unreachable, [], 'A <pre> scrolls in its own box, so it carries tabindex="0".')

    def test_no_summary_holds_a_control(self):
        nested = [
            f"{name}: {' '.join(body.split())[:120]}"
            for name, path in template_files()
            for body in _SUMMARY.findall(path.read_text(encoding="utf-8"))
            if _CONTROL.search(body)
        ]
        self.assertEqual(nested, [], "A summary opens its disclosure; a link it leads to goes in the body.")
