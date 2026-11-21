"""audit:hand-drawn finds what each class says, and places a part where its pages draw it."""

from django.test import SimpleTestCase

from hq.platform.core.hand_drawn import Source
from hq.platform.core.template_parts import Part
from tools import audit_hand_drawn


class HandDrawnTests(SimpleTestCase):
    def test_each_counted_shape_finds_its_examples_and_keeps_the_rest(self) -> None:
        for shape in (*audit_hand_drawn.COUNTED, audit_hand_drawn.BUTTON):
            for source in shape.refuses:
                with self.subTest(shape.name, found=source):
                    self.assertTrue(list(shape.find(Source(source))))
            for source in shape.keeps:
                with self.subTest(shape.name, kept=source):
                    self.assertEqual(list(shape.find(Source(source))), [])

    def test_a_part_is_placed_on_the_pages_that_draw_it_through_every_partial_between(self) -> None:
        templates = {
            "example/page.html": '{% extends "page.html" %}{% include "partials/_example_wrap.html" %}',
            "partials/_example_wrap.html": '{% include "partials/_example.html" %}',
            "partials/_example.html": "<p>A</p>",
            "base.html": '{% include "partials/_example_chrome.html" %}',
            "partials/_example_chrome.html": "<p>B</p>",
            "partials/_example_fragment.html": "<p>C</p>",
            "page.html": "",
        }
        modules = {"hq/domains/example/views.py": 'template_name = "partials/_example_fragment.html"'}
        parts = [
            Part(name="", partial=f"partials/_example{suffix}.html", about="An example.")
            for suffix in ("", "_chrome", "_fragment")
        ]
        places = audit_hand_drawn._places(templates, modules, parts)
        self.assertEqual(places("partials/_example.html"), {"example/page.html"})
        self.assertEqual(places("partials/_example_chrome.html"), {"every page"})
        self.assertEqual(places("partials/_example_fragment.html"), {"hq/domains/example/views.py"})
