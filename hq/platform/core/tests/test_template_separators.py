"""A mark drawn between or beside words (a middle dot, an arrow) never opens
a line or stands on one alone, and a reader never hears it.

A dot between two parts of a line is `{% dot %}`, a run of whole parts a
`.dot-list`, the arrow that ends a link onward `{% arrow %}`; each holds its
mark to the word before it and hides it from a reader. Any other mark is the
text of an element a reader never hears (`aria-hidden`), a control named
otherwise (`aria-label`) or a key (`<kbd>`). Every template Django loads
is read, an extension's too, so the plugin check holds them as the host's.
A stylesheet's generated marks are `lint:css`'s.
"""

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import template_files
from hq.platform.core.tests.separators import loose_dots, typed_marks


class SeparatorRuleTests(SimpleTestCase):
    """What the checks read as a mark out of place."""

    def test_a_dot_typed_between_two_parts_is_found(self):
        self.assertEqual(typed_marks("<p>{{ a }} · {{ b }}</p>"), [(1, "·")])
        self.assertEqual(typed_marks("<p>\n{{ a }}{% if b %} &middot; {{ b }}{% endif %}</p>"), [(2, "·")])

    def test_an_arrow_a_reader_hears_is_found(self):
        self.assertEqual(typed_marks('<a href="{{ url }}">All &rarr;</a>'), [(1, "→")])

    def test_an_arrow_a_reader_never_hears_passes(self):
        source = '<span aria-hidden="true">→</span><a aria-label="Next month" href="{{ url }}">&rsaquo;</a><kbd>↑</kbd>'
        self.assertEqual(typed_marks(source), [])

    def test_a_mark_no_line_lays_out_passes(self):
        self.assertEqual(typed_marks('<span data-tip="{{ a }} · {{ b }}">{{ a }}</span>'), [])
        self.assertEqual(typed_marks("<title>{{ a }} · HQ</title>{# a · b #}"), [])
        self.assertEqual(typed_marks("{% comment %}\na · b → c\n{% endcomment %}"), [])

    def loose(self, source):
        return loose_dots({"example/page.html": source})

    def test_a_mark_straight_after_its_part_passes(self):
        source = (
            "{{ a }}{% if b %}{% dot %}{{ b }}{% endif %}"
            '<span class="muted">{% dot %}d</span><a href="{{ url }}">All{% arrow %}</a>'
            "{% for x in xs %}{{ x }}{% if not forloop.last %}, {% endif %}{% endfor %}{% dot %}e"
            "{% for x in xs %}{% if not forloop.first %}{% dot %}{% endif %}{{ x }}{% endfor %}"
        )
        self.assertEqual(self.loose(source), [])

    def test_a_mark_a_line_may_start_with_is_found(self):
        self.assertEqual(self.loose("{{ a }} {% if b %}{% dot %}{{ b }}{% endif %}"), ["example/page.html:1"])
        self.assertEqual(self.loose("{{ a }}\n  {% dot %}{{ b }}"), ["example/page.html:2"])
        self.assertEqual(self.loose("<p>{{ a }}</p>{% dot %}b"), ["example/page.html:1"])
        self.assertEqual(self.loose('<a href="{{ url }}">All {% arrow %}</a>'), ["example/page.html:1"])

    def test_a_dot_after_any_branch_that_ends_in_a_space_is_found(self):
        source = "{{ a }}{% if b %}{{ b }}{% else %}\n{% endif %}{% dot %}c"
        self.assertEqual(self.loose(source), ["example/page.html:2"])
        source = "<p>{% if a %}{{ a }}{% endif %}{% if b %}{% dot %}{{ b }}{% endif %}</p>"
        self.assertEqual(self.loose(source), ["example/page.html:1"])

    def test_a_dot_asked_on_the_condition_that_drew_its_part_passes(self):
        source = (
            "<p>{% if a %}{{ a }}{% endif %}{% if a and b %}{% dot %}{% endif %}{% if b %}{{ b }}{% endif %}</p>"
            "<p>{% if a.x and a.y %}{{ a }}{% elif c %}{{ c }}{% endif %}{% if d %}{% if a.x and a.y or c %}{% dot %}"
            "{% endif %}{{ d }}{% endif %}</p>"
        )
        self.assertEqual(self.loose(source), [])

    def test_a_dot_is_read_where_an_include_draws_it(self):
        found = self.loose('{{ a }}\n{% include "partials/_sweep_freshness.html" with inventory=x only_kind="k" %}')
        self.assertTrue(found and all(site.startswith("partials/_sweep_freshness.html:") for site in found), found)
        source = '{{ a }}\n{% include "partials/_sweep_freshness.html" with inventory=x only_kind="k" inline=False %}'
        self.assertEqual(self.loose(source), [])


@tag("source", "tree")
class TemplateSeparatorTests(SimpleTestCase):
    def test_no_template_types_a_mark_a_reader_hears(self):
        found = [
            f"{name}:{line}: {mark}"
            for name, path in template_files()
            for line, mark in typed_marks(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(
            found,
            [],
            "a dot between two parts of a line is {% dot %}, a run of parts a .dot-list, a link's onward arrow"
            " {% arrow %}, and any other mark the text of an aria-hidden element",
        )

    def test_every_held_mark_follows_its_part(self):
        found = loose_dots({name: path.read_text(encoding="utf-8") for name, path in template_files()})
        self.assertEqual(
            found, [], "{% dot %} and {% arrow %} are written straight after the part before them, never after a space"
        )
