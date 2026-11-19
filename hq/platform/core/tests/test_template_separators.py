"""A held mark (`{% dot %}`, `{% arrow %}`) never opens a line: it is
written straight after the part before it. A mark typed as text is the
typed-mark shape's (hq/platform/core/hand_drawn.py). Every template Django
loads is read, an extension's too.
"""

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.source import template_files
from hq.platform.core.tests.separators import loose_dots


class SeparatorRuleTests(SimpleTestCase):
    """What the check reads as a held mark a line may start with."""

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
    def test_every_held_mark_follows_its_part(self):
        found = loose_dots({name: path.read_text(encoding="utf-8") for name, path in template_files()})
        self.assertEqual(
            found, [], "{% dot %} and {% arrow %} are written straight after the part before them, never after a space"
        )
