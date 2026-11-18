"""A figure says which way it went and where its line leads, through the one band that draws it."""

from django.template import Context, Template
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, with_class


def _band(*items, size="large") -> str:
    return Template('{% facts layout="band" size=size items=items %}').render(Context({"items": items, "size": size}))


class KpiToneTests(SimpleTestCase):
    def test_a_change_is_drawn_in_the_tone_of_the_way_it_went_at_any_size(self):
        from ..card_items import Kpi

        for size in ("large", "lead"):
            with self.subTest(size=size):
                drawn = _band(Kpi("Net worth", "$1,000"), Kpi("Since yesterday", "+$120", tone="good"), size=size)
                (toned,) = (fact for fact in with_class(drawn, "fact") if "tone-good" in fact.classes)
                (value,) = (value for value in with_class(drawn, "fact-value") if toned in value.ancestors)
                self.assertEqual(value.words, "+$120")

    def test_the_line_under_a_level_figure_says_its_movement_in_its_tone(self):
        from ..card_items import Kpi

        drawn = _band(Kpi("Net worth", "$1,000", "−$40 in 30 days", detail_tone="attention"))

        (toned,) = with_class(drawn, "toned")
        self.assertEqual(toned.words, "−$40 in 30 days")
        self.assertIn("toned-attention", toned.classes)
        (detail,) = with_class(drawn, "fact-detail")
        self.assertIn(detail, toned.ancestors)

    def test_a_tone_comes_from_the_one_vocabulary(self):
        from ..card_items import Kpi

        for field in ("tone", "detail_tone"):
            with self.subTest(field), self.assertRaises(ValueError):
                Kpi("Example", "1", "Example line", **{field: "warning"})


class KpiLinkTests(SimpleTestCase):
    def test_a_line_that_counts_records_leads_to_them(self):
        from ..card_items import Kpi

        drawn = _band(Kpi("Expenses", "$120.00", "3 expenses", detail_url="/example/expenses/"))

        self.assertEqual(
            [(link.attrs["href"], link.words) for link in elements(drawn, "a")], [("/example/expenses/", "3 expenses")]
        )

    def test_a_figure_with_a_page_and_a_linked_line_holds_two_links_never_one_around_another(self):
        from ..card_items import Kpi

        drawn = _band(Kpi("Expenses", "$120.00", "3 expenses", url="/example/", detail_url="/example/expenses/"))

        hrefs = [link.attrs["href"] for link in elements(drawn, "a")]
        self.assertEqual(sorted(hrefs), ["/example/", "/example/expenses/"])
        self.assertTrue(all(outer.tag != "a" for link in elements(drawn, "a") for outer in link.ancestors))
        (value,) = with_class(drawn, "fact-value")
        (link,) = (link for link in elements(drawn, "a") if value in link.ancestors)
        self.assertEqual(link.attrs["href"], "/example/")

    def test_a_line_with_a_link_needs_its_words(self):
        from ..card_items import Kpi

        with self.assertRaises(ValueError):
            Kpi("Expenses", "$120.00", detail_url="/example/expenses/")


class KpiAllocationTests(SimpleTestCase):
    def test_the_parts_of_a_whole_are_drawn_under_it_each_legend_row_a_link_onward(self):
        from ..card_items import Kpi
        from ..drawings import AllocationBar, Share

        bar = AllocationBar(
            "Example assets",
            (Share("Cash", 3, 1, url="/example/cash/"), Share("Owed", 1, 2, url="/example/owed/")),
        )

        drawn = _band(Kpi("Net worth", "$2.00", url="/example/", drawing=bar), size="lead")

        self.assertEqual(len(with_class(drawn, "allocation")), 1)
        hrefs = sorted(link.attrs["href"] for link in elements(drawn, "a"))
        self.assertEqual(hrefs, ["/example/", "/example/cash/", "/example/owed/"])
        self.assertTrue(all(outer.tag != "a" for link in elements(drawn, "a") for outer in link.ancestors))

    def test_a_whole_with_nothing_in_it_draws_no_bar(self):
        from ..card_items import Kpi
        from ..drawings import AllocationBar, Share

        drawn = _band(Kpi("Net worth", "$0.00", drawing=AllocationBar("Example assets", (Share("Cash", 0, 1),))))

        self.assertEqual(with_class(drawn, "allocation"), [])
