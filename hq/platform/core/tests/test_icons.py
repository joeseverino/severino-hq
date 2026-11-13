"""The bar's marks are names in the one icon set, drawing the shapes the bar drew."""

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from hq.platform.application.tests.markup import all_elements

# Each mark of the header and the search dialog, as its shapes: the element
# and the attributes that place it. The set's own "search" stands for both
# magnifiers, so a search looks the same in the bar and in the dialog.
CHROME = {
    "menu": (
        ("rect", {"x": "0", "y": "0", "width": "16", "height": "2", "rx": "1"}),
        ("rect", {"x": "0", "y": "5", "width": "16", "height": "2", "rx": "1"}),
        ("rect", {"x": "0", "y": "10", "width": "16", "height": "2", "rx": "1"}),
    ),
    "caret": (("path", {"d": "M1 1l4 4 4-4"}),),
    "bell": (("path", {"d": "M6 16.5V11a6 6 0 0 1 12 0v5.5l1.5 1.5h-15z"}), ("path", {"d": "M10 20.5a2 2 0 0 0 4 0"})),
    "person": (
        ("circle", {"cx": "12", "cy": "8.2", "r": "3.6"}),
        ("path", {"d": "M5.4 19.4c0-3.7 3-5.9 6.6-5.9s6.6 2.2 6.6 5.9"}),
    ),
    "search": (("circle", {"cx": "11", "cy": "11", "r": "6.5"}), ("path", {"d": "m16 16 4.5 4.5"})),
}
_PLACES = {"x", "y", "width", "height", "rx", "d", "cx", "cy", "r"}


def _shapes(name):
    drawn = all_elements(render_to_string("partials/_icon.html", {"name": name}))
    return tuple(
        (element.tag, {key: value for key, value in element.attrs.items() if key in _PLACES})
        for element in drawn
        if element.tag != "svg"
    )


class ChromeIconTests(SimpleTestCase):
    def test_each_mark_of_the_bar_is_a_name_in_the_set(self):
        for name, shapes in CHROME.items():
            with self.subTest(name):
                self.assertEqual(_shapes(name), shapes)
