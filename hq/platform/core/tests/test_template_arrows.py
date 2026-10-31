"""An arrow a template writes starts or ends a link or a label.

An arrow between two values (a version and its replacement, a mount's source
and destination) is `partials/_change.html`'s, which keeps it on the line of
the value it points at and says the word it stands for. Typed between two
values, it is read aloud as "right arrow" and leaves the line wherever the
line breaks.
"""

import re

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import template_files

ARROW = re.compile(r"→|←|&rarr;|&larr;|&#8594;|&#8592;|&#x2192;|&#x2190;")

# What may stand between an arrow and the edge of the link or label it starts
# or ends: space, a template tag and another arrow (one branch draws one way,
# another the other).
_BETWEEN = rf"(?:\s|\{{%.*?%\}}|{ARROW.pattern})*"
ENDS = re.compile(rf"{_BETWEEN}</(?:a|span)>")
STARTS = re.compile(rf"<(?:a|span)\b[^<>]*>{_BETWEEN}\Z")

# The comment that documents a template, where an arrow is prose.
COMMENT = re.compile(r"\{% comment %\}.*?\{% endcomment %\}|\{#.*?#\}", re.DOTALL)


def stray_arrows(text):
    """The line of each arrow in `text` that neither starts nor ends a link or a label."""

    text = COMMENT.sub(lambda match: re.sub(r"[^\n]", " ", match.group()), text)
    return [
        text.count("\n", 0, match.start()) + 1
        for match in ARROW.finditer(text)
        if not ENDS.match(text, match.end()) and not STARTS.search(text, 0, match.start())
    ]


class StrayArrowTests(SimpleTestCase):
    def test_an_arrow_between_two_values_is_found(self):
        self.assertEqual(stray_arrows("<code>{{ a }}</code> → <code>{{ b }}</code>"), [1])
        self.assertEqual(stray_arrows("<p>\n{{ a }} &rarr; {{ b }}</p>"), [2])

    def test_an_arrow_that_starts_or_ends_a_link_or_a_label_passes(self):
        self.assertEqual(stray_arrows('<a href="/next/">Next &rarr;</a>'), [])
        self.assertEqual(stray_arrows('<span class="muted">Next &rarr; </span>'), [])
        self.assertEqual(stray_arrows('<a href="?page={{ page }}">&larr; Prev</a>'), [])

    def test_an_arrow_drawn_either_way_passes(self):
        text = '<span aria-hidden="true">{% if out %}&rarr;{% else %}&larr;{% endif %}</span>'
        self.assertEqual(stray_arrows(text), [])

    def test_an_arrow_in_a_comment_passes(self):
        self.assertEqual(stray_arrows("{% comment %}\na → b\n{% endcomment %}{# a → b #}"), [])


@tag("source", "tree")
class TemplateArrowTests(SimpleTestCase):
    def test_no_template_types_an_arrow_between_two_values(self):
        found = [
            f"{name}:{line}"
            for name, path in template_files()
            for line in stray_arrows(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(found, [], "an arrow between two values is partials/_change.html's")
