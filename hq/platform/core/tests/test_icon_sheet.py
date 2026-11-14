"""Every icon a page names is a symbol of static/img/icons.svg, so none draws blank."""

import re
from pathlib import Path
from xml.etree import ElementTree

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.application.path_model import _HOP_ICONS

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
    def test_every_icon_named_in_the_source_is_a_symbol(self):
        sources = [
            *ROOT.glob("templates/**/*.html"),
            *(path for path in ROOT.glob("hq/**/*.py") if "tests" not in path.parts),
        ]
        named = {name for path in sources for name in NAMED.findall(path.read_text(encoding="utf-8"))}
        named |= set(_HOP_ICONS.values())

        self.assertGreater(len(named), 10)
        self.assertEqual(sorted(named - symbols().keys()), [])
