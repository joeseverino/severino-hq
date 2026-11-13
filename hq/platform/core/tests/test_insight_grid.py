"""The insight grid's eyebrow says only what the cards around it do not."""

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

    def test_an_eyebrow_its_own_title_holds_is_not_drawn(self):
        (head,) = _eyebrows([Insight("neutral", "Example pace", "Example pace this month", "", "An example body.")])

        self.assertNotIn("Example pace", head)
