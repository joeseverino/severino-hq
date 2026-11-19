from django.template import TemplateSyntaxError
from django.test import SimpleTestCase

from hq.platform.application.markup_testing import elements, with_class
from hq.platform.application.pages import PageAction, RowMark, RowOrder
from hq.platform.core.testing import render


def moves_up(url: str = "/example/7/"):
    """A row's press that moves it up, an offer built where its command was read."""

    (up,) = RowOrder(url, "example 7", up=True, down=False).presses
    return up


class ButtonTagTests(SimpleTestCase):
    """The one tag that draws a button refuses what it would drop or draw wrong."""

    def test_a_link_is_drawn_with_its_look(self):
        html = render('{% button "Example" "/example/" look="primary" %}')

        self.assertIn('href="/example/"', html)
        self.assertIn('class="btn primary"', html)

    def test_a_row_of_a_menu_or_a_link_takes_no_look_that_paints_a_box(self):
        for drawn in ("menu", "link"):
            with self.subTest(drawn):
                html = render(f'{{% button "Example" "/example/" look="primary ghost danger {drawn}" %}}')
                classes = set(html.split('class="')[1].split('"')[0].split())

                self.assertLessEqual({"btn", drawn, "danger"}, classes)
                self.assertFalse({"primary", "ghost"} & classes)

    def test_a_look_it_does_not_offer_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            render('{% button "Example" "/example/" look="invented" %}')

    def test_a_look_and_a_class_together_are_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            render('{% button "Example" "/example/" look="ghost" css="example-pin" %}')

    def test_an_attribute_it_does_not_draw_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            render('{% button "Example" "/example/" color="red" %}')

    def test_a_posting_button_refuses_a_hook_it_would_drop(self):
        with self.assertRaises(TemplateSyntaxError):
            render('{% button "Example" offer=press data_example=True %}', press=moves_up())

    def test_its_icon_and_count_are_drawn_as_markup_beside_its_words(self):
        html = render('{% button "Example" icon="info" count=3 %}')

        self.assertIn('<svg class="icon"', html)
        self.assertIn('<span class="filter-count">3</span>', html)

    def test_a_glyph_with_an_icon_shows_the_icon_and_is_named_by_its_words(self):
        html = render('{% button "Topology" "/example/" look="ghost glyph" icon="info" %}')

        (link,) = elements(html, "a")
        self.assertEqual(link.words, "Topology")
        self.assertEqual([each.words for each in with_class(html, "visually-hidden")], ["Topology"])
        self.assertEqual(len(elements(html, "svg")), 1)

    def test_a_button_out_of_hq_opens_as_every_outside_link_does(self):
        html = render(
            '{% button "↗" url look="glyph" outside=True title="Open live page" %}', url="https://example.com/"
        )

        (link,) = elements(html, "a")
        self.assertEqual((link.attrs["href"], link.attrs["target"]), ("https://example.com/", "_blank"))
        self.assertEqual(set(link.attrs["rel"].split()), {"noopener", "noreferrer"})
        self.assertEqual(link.attrs["aria-label"], "Open live page")
        self.assertLessEqual({"btn", "glyph"}, link.classes)
        self.assertEqual(elements(render('{% button "Open" url outside=True %}', url="javascript:alert(1)"), "a"), [])

    def test_a_button_out_of_hq_refuses_what_an_outside_link_cannot_carry(self):
        for written in (
            '{% button "Open" outside=True %}',
            '{% button "Open" "https://example.com/" outside=True data_example=1 %}',
        ):
            with self.subTest(written), self.assertRaises(TemplateSyntaxError):
                render(written)


class PostingButtonTests(SimpleTestCase):
    """A button posts only what an offer sends, through the one shared form,
    so no page draws a press the rule behind it was not asked about."""

    def test_a_template_cannot_post_to_an_address_of_its_own(self):
        with self.assertRaises(TemplateSyntaxError):
            render('{% button "Example" "/example/" post=True %}')

    def test_a_link_is_no_offer_to_post(self):
        with self.assertRaises(TemplateSyntaxError):
            render('{% button "Example" offer=link %}', link=PageAction("Example", "/example/"))

    def test_it_sends_only_what_its_offer_says(self):
        for written in (
            '{% button "Example" "/example/" offer=press %}',
            '{% button "Example" offer=press value="down" %}',
        ):
            with self.subTest(written), self.assertRaises(TemplateSyntaxError):
                render(written, press=moves_up())

    def test_it_targets_the_shared_form_with_its_offers_address_and_field(self):
        html = render('{% button press.label offer=press look="glyph" %}', press=moves_up("/things/7/"))

        self.assertIn('form="hq-post"', html)
        self.assertIn('formaction="/things/7/"', html)
        self.assertIn('name="direction" value="up"', html)
        self.assertIn('aria-label="Move example 7 up"', html)
        self.assertNotIn("disabled", html)

    def test_a_button_of_words_is_named_by_them(self):
        press = RowMark("/example/pin/", False, "example", "favorites").press
        html = render('{% button "Pin" offer=press %}', press=press)

        self.assertIn('title="Add example to favorites"', html)
        self.assertNotIn("aria-label", html)

    def test_it_can_be_disabled(self):
        html = render('{% button "Example" offer=press disabled=True %}', press=moves_up())
        self.assertIn(" disabled>", html)

    def test_its_label_is_escaped(self):
        html = render("{% button label offer=press %}", label="<b>", press=moves_up())
        self.assertIn("&lt;b&gt;", html)
