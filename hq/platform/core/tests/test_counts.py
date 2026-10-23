"""A bare figure counting what a heading heads stands in that heading, after
its words (`<h2>Readings <span class="count">7</span></h2>`), so every count
reads in one place. What stands at a section head's end is a line about the
section, a count with its unit among them; a bare figure there is cut off
from the words it counts."""

import re
from pathlib import Path

from django.test import SimpleTestCase, tag

TEMPLATES = Path(__file__).resolve().parents[4] / "templates"

# A heading closed, then (perhaps behind a condition) a count that is one
# value with no unit.
_BARE_AFTER_HEADING = re.compile(
    r"</h[1-6]>\s*(?:\{%[^%]*%\}\s*)?<span class=\"count\"[^>]*>\s*\{\{(?![^}]*\bcounted\b)[^}]*\}\}\s*</span>"
)
# A count in parentheses after its words: "Other settings (3)".
_IN_PARENTHESES = re.compile(r"\(\{\{[^}]*\|length\s*\}\}\)")


def misplaced_counts(source: str) -> int:
    source = re.sub(r"<option\b.*?</option>", "", source, flags=re.DOTALL)
    return len(_BARE_AFTER_HEADING.findall(source)) + len(_IN_PARENTHESES.findall(source))


@tag("source")
class CountPlacementTests(SimpleTestCase):
    def test_every_bare_count_stands_in_its_heading(self):
        found = {
            path.relative_to(TEMPLATES).as_posix(): count
            for path in sorted(TEMPLATES.rglob("*.html"))
            if (count := misplaced_counts(path.read_text(encoding="utf-8")))
        }
        self.assertEqual(found, {}, 'Write the count in its heading: <h2>Words <span class="count">N</span></h2>.')

    def test_a_count_cut_off_from_its_words_is_refused(self):
        self.assertEqual(misplaced_counts('<h2>Readings</h2><span class="count">{{ rows|length }}</span>'), 1)
        self.assertEqual(misplaced_counts('<h2>A</h2>\n  {% if n %}<span class="count">{{ n }}</span>{% endif %}'), 1)
        self.assertEqual(misplaced_counts("<summary>Other settings ({{ rows|length }})</summary>"), 1)

    def test_a_count_in_its_heading_or_a_line_with_its_unit_passes(self):
        self.assertEqual(misplaced_counts('<h2>Readings <span class="count">{{ rows|length }}</span></h2>'), 0)
        self.assertEqual(misplaced_counts('<h2>A</h2><span class="count">{{ rows|length|counted:"event" }}</span>'), 0)
        self.assertEqual(misplaced_counts('<h2>A</h2><span class="count">{{ n }} left</span>'), 0)
        self.assertEqual(misplaced_counts("<option>Machines ({{ rows|length }})</option>"), 0)
