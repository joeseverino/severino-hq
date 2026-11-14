"""Every icon is drawn once, as a symbol of static/img/icons.svg, and a page names it.

A page carries a short reference per icon rather than its drawing, which it
would repeat on every row and in every header; the guard here keeps a
drawing from coming back into the icon part.
"""

import re
from pathlib import Path
from xml.etree import ElementTree

from django.conf import settings
from django.template.loader import render_to_string
from django.templatetags.static import static
from django.test import SimpleTestCase, tag

from hq.platform.application.path_model import _HOP_ICONS
from hq.platform.application.tests.markup import all_elements

ROOT = Path(settings.BASE_DIR)
SHEET = ROOT / "static/img/icons.svg"
SVG = "{http://www.w3.org/2000/svg}"
# An icon named in place: the icon part's own include, or an `icon=` argument
# of a part or a record that draws one.
NAMED = re.compile(r'(?:_icon\.html" with name|\bicon)="([a-z][a-z-]*)"')


def symbols() -> dict[str, ElementTree.Element]:
    return {symbol.get("id"): symbol for symbol in ElementTree.parse(SHEET).getroot().iter(f"{SVG}symbol")}  # noqa: S314 - HQ's own sheet, in the checkout


@tag("source")
class IconSheetTests(SimpleTestCase):
    def test_the_icon_part_names_its_symbol_and_draws_nothing_itself(self):
        for name in symbols():
            with self.subTest(name=name):
                drawn = all_elements(render_to_string("partials/_icon.html", {"name": name}))
                self.assertEqual([element.tag for element in drawn], ["svg", "use"])
                self.assertEqual(drawn[1].attrs["href"], f"{static('img/icons.svg')}#{name}")

    def test_each_symbol_is_named_once_and_scaled_from_its_own_box(self):
        sheet = ElementTree.parse(SHEET)  # noqa: S314 - HQ's own sheet, in the checkout
        names = [symbol.get("id") for symbol in sheet.getroot().iter(f"{SVG}symbol")]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual([name for name, symbol in symbols().items() if not symbol.get("viewBox")], [])

    def test_every_icon_named_in_the_source_is_a_symbol(self):
        sources = [
            *ROOT.glob("templates/**/*.html"),
            *(path for path in ROOT.glob("hq/**/*.py") if "tests" not in path.parts),
        ]
        named = {name for path in sources for name in NAMED.findall(path.read_text(encoding="utf-8"))}
        named |= set(_HOP_ICONS.values())

        self.assertGreater(len(named), 10)
        self.assertEqual(sorted(named - symbols().keys()), [])
