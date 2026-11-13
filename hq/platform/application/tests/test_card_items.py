"""A figure says which way it went and where its line leads, through the one grid that draws it."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from .markup import elements, with_class


def _grid(*items, featured=False) -> str:
    return render_to_string("partials/_kpi_grid.html", {"items": items, "featured": featured})


class KpiToneTests(SimpleTestCase):
    def test_a_change_is_drawn_in_the_tone_of_the_way_it_went_in_a_featured_row_too(self):
        from ..card_items import Kpi

        for featured in (False, True):
            with self.subTest(featured=featured):
                drawn = _grid(
                    Kpi("Net worth", "$1,000"), Kpi("Since yesterday", "+$120", tone="good"), featured=featured
                )
                (toned,) = with_class(drawn, "toned")
                self.assertEqual(toned.words, "+$120")
                self.assertIn("toned-good", toned.classes)
                self.assertIn("value", toned.ancestors[-1].classes)

    def test_the_line_under_a_level_figure_says_its_movement_in_its_tone(self):
        from ..card_items import Kpi

        drawn = _grid(Kpi("Net worth", "$1,000", "−$40 in 30 days", detail_tone="attention"))

        (toned,) = with_class(drawn, "toned")
        self.assertEqual(toned.words, "−$40 in 30 days")
        self.assertIn("toned-attention", toned.classes)
        self.assertNotIn("toned", {name for value in with_class(drawn, "value") for name in value.classes})

    def test_a_tone_comes_from_the_one_vocabulary(self):
        from ..card_items import Kpi

        for field in ("tone", "detail_tone"):
            with self.subTest(field), self.assertRaises(ValueError):
                Kpi("Example", "1", "Example line", **{field: "warning"})


class KpiLinkTests(SimpleTestCase):
    def test_a_line_that_counts_records_leads_to_them(self):
        from ..card_items import Kpi

        drawn = _grid(Kpi("Expenses", "$120.00", "3 expenses", detail_url="/example/expenses/"))

        self.assertEqual(
            [(link.attrs["href"], link.words) for link in elements(drawn, "a")], [("/example/expenses/", "3 expenses")]
        )

    def test_a_figure_holding_a_link_is_reached_through_its_value_never_a_link_around_a_link(self):
        """A link does not hold links: the cell is no link, its value and its line are."""

        from ..card_items import Kpi

        drawn = _grid(Kpi("Expenses", "$120.00", "3 expenses", url="/example/", detail_url="/example/expenses/"))

        hrefs = [link.attrs["href"] for link in elements(drawn, "a")]
        self.assertEqual(sorted(hrefs), ["/example/", "/example/expenses/"])
        self.assertTrue(all(outer.tag != "a" for link in elements(drawn, "a") for outer in link.ancestors))
        (value,) = with_class(drawn, "value")
        self.assertEqual((value.tag, value.attrs.get("href")), ("a", "/example/"))

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

        drawn = _grid(Kpi("Net worth", "$2.00", url="/example/", drawing=bar), featured=True)

        self.assertEqual(len(with_class(drawn, "allocation")), 1)
        hrefs = sorted(link.attrs["href"] for link in elements(drawn, "a"))
        self.assertEqual(hrefs, ["/example/", "/example/cash/", "/example/owed/"])
        self.assertTrue(all(outer.tag != "a" for link in elements(drawn, "a") for outer in link.ancestors))

    def test_a_whole_with_nothing_in_it_draws_no_bar(self):
        from ..card_items import Kpi
        from ..drawings import AllocationBar, Share

        drawn = _grid(Kpi("Net worth", "$0.00", drawing=AllocationBar("Example assets", (Share("Cash", 0, 1),))))

        self.assertEqual(with_class(drawn, "allocation"), [])
