from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.pages import PageAction, RowMark, RowOrder


def draw(source: str, **context) -> str:
    return Template(source).render(Context(context))


def moves_up(url: str = "/example/7/"):
    """A row's press that moves it up, an offer built where its command was read."""

    (up,) = RowOrder(url, "example 7", up=True, down=False).presses
    return up


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
            draw('{% button "Example" offer=press data_example=True %}', press=moves_up())

    def test_its_icon_and_count_are_drawn_as_markup_beside_its_words(self):
        html = draw('{% button "Example" icon="info" count=3 %}')

        self.assertIn('<svg class="icon"', html)
        self.assertIn('<span class="filter-count">3</span>', html)


class PostingButtonTests(SimpleTestCase):
    """A button posts only what an offer sends, through the one shared form,
    so no page draws a press the rule behind it was not asked about."""

    def test_a_template_cannot_post_to_an_address_of_its_own(self):
        with self.assertRaises(TemplateSyntaxError):
            draw('{% button "Example" "/example/" post=True %}')

    def test_a_link_is_no_offer_to_post(self):
        with self.assertRaises(TemplateSyntaxError):
            draw('{% button "Example" offer=link %}', link=PageAction("Example", "/example/"))

    def test_it_sends_only_what_its_offer_says(self):
        for written in (
            '{% button "Example" "/example/" offer=press %}',
            '{% button "Example" offer=press value="down" %}',
        ):
            with self.subTest(written), self.assertRaises(TemplateSyntaxError):
                draw(written, press=moves_up())

    def test_it_targets_the_shared_form_with_its_offers_address_and_field(self):
        html = draw('{% button press.label offer=press css="icon-link" %}', press=moves_up("/things/7/"))

        self.assertIn('form="hq-post"', html)
        self.assertIn('formaction="/things/7/"', html)
        self.assertIn('name="direction" value="up"', html)
        self.assertIn('aria-label="Move example 7 up"', html)
        self.assertNotIn("disabled", html)

    def test_a_button_of_words_is_named_by_them(self):
        press = RowMark("/example/pin/", False, "example", "favorites").press
        html = draw('{% button "Pin" offer=press %}', press=press)

        self.assertIn('title="Add example to favorites"', html)
        self.assertNotIn("aria-label", html)

    def test_it_can_be_disabled(self):
        html = draw('{% button "Example" offer=press disabled=True %}', press=moves_up())
        self.assertIn(" disabled>", html)

    def test_its_label_is_escaped(self):
        html = draw("{% button label offer=press %}", label="<b>", press=moves_up())
        self.assertIn("&lt;b&gt;", html)
