"""The insight grid's eyebrow, which carries each card's state, is left out only as a repeat."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.platform.application.card_items import Insight
from hq.platform.application.tests.markup import with_class


def _eyebrows(items: list[Insight]) -> list[str]:
    html = render_to_string("partials/_insight_grid.html", {"items": items})
    return [head.words for head in with_class(html, "insight-head")]


class InsightEyebrowTests(SimpleTestCase):
    def test_an_eyebrow_the_card_before_showed_is_not_drawn_again(self):
        heads = _eyebrows(
            [
                Insight("attention", "Example source", "Example records are late", "", "An example body."),
                Insight("attention", "Example source", "Example totals are stale", "", "An example body."),
                Insight("serious", "Example source", "Example import failed", "", "An example body."),
            ]
        )

        self.assertIn("Example source", heads[0])
        self.assertNotIn("Example source", heads[1])
        # A new state is news, so its pill says it.
        self.assertIn("Example source", heads[2])

    def test_a_card_whose_title_holds_its_eyebrow_still_says_its_state(self):
        # The pill is the only place a card says its state, so a title that
        # repeats its words never takes it away.
        (head,) = _eyebrows([Insight("attention", "Example pace", "Example pace this month", "", "An example body.")])

        self.assertIn("Example pace", head)


class InsightLevelTests(SimpleTestCase):
    def test_a_grid_under_the_page_title_heads_its_cards_a_level_under_it(self):
        item = Insight("neutral", "Example stage", "Who accepts example mail?", "mx.example.com", "An example body.")

        for level, tag in ((None, "h3"), (2, "h2")):
            with self.subTest(level=level):
                context = {"items": [item]} | ({"level": level} if level else {})
                html = render_to_string("partials/_insight_grid.html", context)
                self.assertEqual([title.tag for title in with_class(html, "insight-title")], [tag])
