from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase


def draw(source: str) -> str:
    return Template(source).render(Context({}))


class ButtonTagTests(SimpleTestCase):
    """The one tag that draws a button refuses what it would drop or draw wrong."""

    def test_a_link_is_drawn_with_its_look(self):
        html = draw('{% button "Example" "/example/" look="primary" %}')

        self.assertIn('href="/example/"', html)
        self.assertIn('class="btn primary"', html)

    def test_a_row_of_a_menu_or_a_link_takes_no_look_that_paints_a_box(self):
        for drawn in ("menu", "link"):
            with self.subTest(drawn):
                html = draw(f'{{% button "Example" "/example/" look="primary ghost danger {drawn}" %}}')
                classes = set(html.split('class="')[1].split('"')[0].split())

                self.assertLessEqual({"btn", drawn, "danger"}, classes)
                self.assertFalse({"primary", "ghost"} & classes)

    def test_a_look_it_does_not_offer_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            draw('{% button "Example" "/example/" look="invented" %}')

    def test_a_look_and_a_class_together_are_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            draw('{% button "Example" "/example/" look="ghost" css="icon-link" %}')

    def test_an_attribute_it_does_not_draw_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            draw('{% button "Example" "/example/" color="red" %}')

    def test_a_posting_button_refuses_a_hook_it_would_drop(self):
        with self.assertRaises(TemplateSyntaxError):
            draw('{% button "Example" "/example/" post=True data_example=True %}')

    def test_its_icon_and_count_are_drawn_as_markup_beside_its_words(self):
        html = draw('{% button "Example" icon="info" count=3 %}')

        self.assertIn('<svg class="icon"', html)
        self.assertIn('<span class="filter-count">3</span>', html)
