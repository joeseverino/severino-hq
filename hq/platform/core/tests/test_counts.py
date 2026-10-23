"""A bare figure counting what a heading heads stands in that heading, after
its words (`<h2>Readings <span class="count">7</span></h2>`), so every count
reads in one place. What stands at a section head's end is a line about the
section, a count with its unit among them; a bare figure there is cut off
from the words it counts.

A count is a value measured with `|length` or named for one (`count`,
`unread_count`, `group.count`), with no `counted` to give it its unit. Any
other value standing alone there is a phrase, a line about the section.

The rule reads every directory Django loads templates from, the host's and
each installed extension's, so an extension's plugin check holds it too."""

import re

from django.template.autoreload import get_template_directories
from django.test import SimpleTestCase

# A heading closed, then (perhaps behind a condition) a span holding one value
# and nothing else.
_ALONE_AFTER_HEADING = re.compile(
    r"</h[1-6]>\s*(?:\{%[^%]*%\}\s*)?<span class=\"count\"[^>]*>\s*\{\{([^}]*)\}\}\s*</span>"
)
# A count in parentheses after its words: "Other settings (3)".
_IN_PARENTHESES = re.compile(r"\(\{\{[^}]*\|length\s*\}\}\)")
_NAMED_COUNT = re.compile(r"(?:^|[._])count$")


def is_bare_count(expression: str) -> bool:
    name, *filters = (part.strip() for part in expression.split("|"))
    filter_names = {part.split(":")[0] for part in filters}
    if "counted" in filter_names:
        return False
    return "length" in filter_names or _NAMED_COUNT.search(name) is not None


def misplaced_counts(source: str) -> int:
    source = re.sub(r"<option\b.*?</option>", "", source, flags=re.DOTALL)
    alone = [value for value in _ALONE_AFTER_HEADING.findall(source) if is_bare_count(value)]
    return len(alone) + len(_IN_PARENTHESES.findall(source))


class CountPlacementTests(SimpleTestCase):
    maxDiff = None

    def test_every_bare_count_stands_in_its_heading(self):
        pages = sorted({page for directory in get_template_directories() for page in directory.rglob("*.html")})
        self.assertTrue(pages, "The count rule found no template to read.")
        found = {str(page): count for page in pages if (count := misplaced_counts(page.read_text(encoding="utf-8")))}
        self.assertEqual(found, {}, 'Write the count in its heading: <h2>Words <span class="count">N</span></h2>.')

    def test_a_count_cut_off_from_its_words_is_refused(self):
        self.assertEqual(misplaced_counts('<h2>Readings</h2><span class="count">{{ rows|length }}</span>'), 1)
        self.assertEqual(
            misplaced_counts('<h2>A</h2>\n  {% if n_count %}<span class="count">{{ n_count }}</span>{% endif %}'), 1
        )
        self.assertEqual(misplaced_counts('<h2>A</h2><span class="count">{{ group.count|breakable }}</span>'), 1)
        self.assertEqual(misplaced_counts("<summary>Other settings ({{ rows|length }})</summary>"), 1)

    def test_a_count_in_its_heading_or_a_line_with_its_unit_passes(self):
        self.assertEqual(misplaced_counts('<h2>Readings <span class="count">{{ rows|length }}</span></h2>'), 0)
        self.assertEqual(misplaced_counts('<h2>A</h2><span class="count">{{ rows|length|counted:"event" }}</span>'), 0)
        self.assertEqual(misplaced_counts('<h2>A</h2><span class="count">{{ n }} left</span>'), 0)
        self.assertEqual(misplaced_counts("<option>Machines ({{ rows|length }})</option>"), 0)

    def test_a_phrase_at_a_section_heads_end_passes(self):
        self.assertEqual(misplaced_counts('<h2>Mile by mile</h2><span class="count">{{ split_summary }}</span>'), 0)
        self.assertEqual(misplaced_counts('<h2>A</h2><span class="count">{{ account }}</span>'), 0)
