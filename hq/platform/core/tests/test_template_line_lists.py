"""No template decides by a count how much of a list a row shows.

A list in a cell folded at a fixed number of parts (`|slice:":3"` then a
`+N` behind `row-less`, the rest in `row-more`) hides parts its line has room
for, and offers to open a row whose parts all fit. How many show is the
layout's: a run of parts is a `{% line_list %}` (table_tags), which shows as
many as the line holds and opens the row only for what it cuts.
"""

import re

from django.template import Context, Template, TemplateSyntaxError
from django.template.base import Lexer, TokenType
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import with_class
from hq.platform.core.tests.test_template_values import template_sources

# What a row holds back until it opens, and the hint standing in for it.
_HELD = re.compile(r"""class=["'][^"']*\b(row-more|row-less)\b""")
# A tag that decides by a count: a loop's place, a list's length, a slice
# at a fixed place, a number taken off, or a comparison with a number.
_COUNT = re.compile(r"""forloop\.counter|\|length\b|\|slice:["']-?\d*:?\d|\|add:["']-\d|[<>]=?\s*\d""")
_OPENS = frozenset(("if", "for", "with"))


def counted_folds(source: str) -> list[str]:
    """Each piece of markup that holds a part back inside a tag deciding by a count."""

    found: list[str] = []
    opened: list[tuple[str, bool]] = []
    for token in Lexer(source).tokenize():
        if token.token_type == TokenType.BLOCK:
            name = token.contents.split()[0] if token.contents.split() else ""
            if name in _OPENS:
                opened.append((name, bool(_COUNT.search(token.contents))))
            elif name in ("elif", "else", "empty") and opened:
                was, counted = opened.pop()
                opened.append((was, counted or bool(_COUNT.search(token.contents))))
            elif name.startswith("end") and name[3:] in _OPENS and opened:
                opened.pop()
        elif token.token_type == TokenType.TEXT and any(counted for _, counted in opened):
            found += [match.group(0) for match in _HELD.finditer(token.contents)]
    return found


@tag("source", "tree")
class LineListSourceTests(SimpleTestCase):
    def test_no_template_folds_a_list_at_a_count(self):
        found = {name: held for name, source in template_sources().items() if (held := counted_folds(source))}
        self.assertEqual(found, {}, "draw the list with {% line_list %}, which the layout cuts, never a count")


class CountedFoldTests(SimpleTestCase):
    """What the check reads as a fold decided by a count."""

    def test_a_slice_with_its_rest_held_back_is_found(self):
        source = (
            '{% for t in tags|slice:":3" %}{{ t }}{% endfor %}{% if tags|length > 3 %}'
            '<span class="muted row-less">+1</span><span class="row-more">x</span>{% endif %}'
        )
        self.assertEqual(len(counted_folds(source)), 2)

    def test_a_loop_place_marking_a_part_held_back_is_found(self):
        source = '{% for link in links %}<li{% if forloop.counter > 3 %} class="row-more"{% endif %}>x</li>{% endfor %}'
        self.assertEqual(counted_folds(source), ['class="row-more'])

    def test_a_count_named_once_and_compared_later_is_found(self):
        source = '{% with folded=reads|length|add:"-4" %}{% if folded > 0 %}<span class="row-less">+</span>{% endif %}{% endwith %}'
        self.assertEqual(counted_folds(source), ['class="row-less'])

    def test_a_part_held_back_for_what_it_is_passes(self):
        source = (
            '{% if row.note %}<span class="row-more">{{ row.note }}</span>{% endif %}<a class="row-less" href="#">x</a>'
        )
        self.assertEqual(counted_folds(source), [])

    def test_a_line_list_passes(self):
        self.assertEqual(counted_folds("{% line_list tags as t %}{{ t }}{% endline_list %}"), [])


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

    def test_a_list_class_draws_a_list_whose_rest_is_an_item(self):
        markup, with_class = self.drawn(
            '{% line_list names as name list="dot-list" %}<li>{{ name }}</li>{% endline_list %}', names="abcd"
        )
        (rest,) = with_class(markup, "line-rest")
        self.assertEqual(rest.tag, "li")
        self.assertTrue(markup.startswith('<ul class="dot-list line-list">'))

    def test_a_value_is_escaped_by_the_body_that_draws_it(self):
        markup, _ = self.drawn("{% line_list names as name %}{{ name }}{% endline_list %}", names=["<i>"])
        self.assertIn("&lt;i&gt;", markup)

    def test_a_wrong_form_is_refused(self):
        for source in (
            "{% line_list names %}{% endline_list %}",
            '{% line_list names as n css="x" %}{% endline_list %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                Template(source)
