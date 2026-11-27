"""A run of parts is a `{% line_list %}` (list_tags), which shows as many as
the line holds and opens the row only for what it cuts.
"""

import re

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import with_class


class LineListTagTests(SimpleTestCase):
    """What `{% line_list %}` draws: every part, the ones past its lead in the rest."""

    def drawn(self, source, **context):
        return Template(source).render(Context(context)), with_class

    def test_every_part_is_drawn_and_those_past_the_lead_are_the_rest(self):
        markup, with_class = self.drawn(
            "{% line_list names as name %}<b>{{ name }}</b>{% endline_list %}", names=["a", "b", "c", "d", "e"]
        )
        (rest,) = with_class(markup, "line-rest")
        self.assertEqual(markup.count("<b>"), 5)
        self.assertEqual(re.findall(r"<b>(\w)</b>", markup.split('class="line-rest"')[1]), ["d", "e"])
        self.assertTrue(rest.words)
        self.assertNotIn("row-more", markup)

    def test_a_short_list_has_no_rest_and_an_empty_one_draws_nothing(self):
        markup, with_class = self.drawn("{% line_list names as name %}{{ name }}{% endline_list %}", names=["a", "b"])
        self.assertEqual(with_class(markup, "line-rest"), [])
        self.assertEqual(self.drawn("{% line_list names as n %}{{ n }}{% endline_list %}", names=[])[0], "")

    def test_a_list_kind_draws_that_list_whose_rest_is_an_item(self):
        markup, with_class = self.drawn(
            '{% line_list names as name kind="dots" %}<li>{{ name }}</li>{% endline_list %}', names="abcd"
        )
        (rest,) = with_class(markup, "line-rest")
        self.assertEqual(rest.tag, "li")
        self.assertTrue(markup.startswith('<ul class="dot-list line-list">'))

    def test_a_value_is_escaped_by_the_body_that_draws_it(self):
        markup, _ = self.drawn("{% line_list names as name %}{{ name }}{% endline_list %}", names=["<i>"])
        self.assertIn("&lt;i&gt;", markup)

    def test_values_with_no_name_to_draw_them_by_are_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template("{% line_list names %}{% endline_list %}")
