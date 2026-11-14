"""A page that is one document under the header is drawn by one template and
filled by one rule, so the API reference and the Design page cannot drift
apart, and a third such page is the same page."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from hq.platform.core.stylesheets import STATIC

ROOT = Path(settings.BASE_DIR)
# The one template that empties the footer, and the one sheet that fills the view.
PRIMITIVE = "templates/document_page.html"
FILL_SHEET = "css/app/page.css"
EMPTIED_FOOTER = re.compile(r"{%\s*block\s+site_footer\s*%}\s*{%\s*endblock")
RULE = re.compile(r"([^{}]+)\{([^{}]*)\}")
# What makes a rule the fill: the content column taken edge to edge, or a box
# sized to the view under the header, for a page by what its column holds.
# Paper takes the column edge to edge for every page (cadence.css), which is
# no page's fill.
FILLS = re.compile(r"max-width\s*:\s*none|(?<![-\w])padding\s*:\s*0\s*;|100dvh\s*-\s*var\(--site-header-height")


@tag("source", "tree")
@reads("**/templates/**", "static/css/**")
class DocumentPageTests(SimpleTestCase):
    def test_only_the_document_page_empties_the_footer(self):
        found = sorted(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.glob("**/templates/**/*.html")
            if "node_modules" not in path.parts and EMPTIED_FOOTER.search(path.read_text(encoding="utf-8"))
        )

        self.assertEqual(found, [PRIMITIVE], "a page that is one document extends document_page.html")

    def test_only_the_document_page_s_rule_fills_the_view_from_the_content_column(self):
        found = []
        for path in sorted((STATIC / "css").rglob("*.css")):
            css = re.sub(r"/\*.*?\*/", " ", path.read_text(encoding="utf-8"), flags=re.DOTALL)
            for selector, body in RULE.findall(css):
                name = " ".join(selector.split())
                if (
                    re.search(r"\.content\s*(?::has|>)", name)
                    and FILLS.search(body)
                    and "[data-document-page]" not in name
                ) or (
                    "[data-document-page]" in name
                    and FILLS.search(body)
                    and path.relative_to(STATIC).as_posix() != FILL_SHEET
                ):
                    found.append(f"{path.relative_to(STATIC)}: {name}")

        self.assertEqual(found, [], "a page that is one document takes the fill of [data-document-page] (page.css)")
