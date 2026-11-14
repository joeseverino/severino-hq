"""Title meta: the facts on a page's title line are one part."""

import inspect
import re
from pathlib import Path

from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, tag

from hq.platform.application.tests.markup import elements, with_class
from hq.platform.core.tests.test_part_tags import _catalog_templates


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class TitleMetaTagTests(SimpleTestCase):
    def test_its_parts_are_a_lists_items_with_the_pages_hooks(self):
        html = _render(
            "{% title_meta data_record_status=state %}<li>Current</li><li><code>{{ host }}</code></li>{% endtitle_meta %}",
            state="current",
            host="example-host",
        )

        (meta,) = with_class(html, "page-title-meta")
        self.assertEqual(meta.tag, "ul")
        self.assertIn("dot-list", meta.classes)
        self.assertEqual(meta.attrs["data-record-status"], "current")
        self.assertEqual([item.words for item in elements(html, "li")], ["Current", "example-host"])

    def test_words_outside_a_part_are_refused(self):
        """Words between the parts would join the line with no dot before
        them, and a dot written by hand can open a line."""

        for parts in ("Online{% dot %}<code>example-host</code>", "<li>Online</li> since today", ""):
            with self.subTest(parts=parts), self.assertRaises(TemplateSyntaxError):
                _render(f"{{% title_meta %}}{parts}{{% endtitle_meta %}}")

    def test_an_option_it_does_not_take_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{% title_meta css="example" %}<li>A</li>{% endtitle_meta %}')


@tag("source")
class TitleMetaCatalogTests(SimpleTestCase):
    def test_every_option_of_title_meta_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        option it takes is drawn there at least once. An entry draws it as a
        page head's aside, so its tags are read from the part family's
        source."""

        from hq.platform.core.templatetags.title_tags import register
        from hq.platform.core.tests.browser.catalog.families import part

        takes = register.tags["title_meta"].__wrapped__
        options = [
            option
            for option, parameter in inspect.signature(takes).parameters.items()
            if parameter.kind is inspect.Parameter.KEYWORD_ONLY
        ]
        entries = " ".join(t for name, t in _catalog_templates().items() if name.startswith("part/title-meta"))
        opened = re.findall(r"\{% title_meta ?(.*?)%\}", Path(part.__file__).read_text(encoding="utf-8"))
        self.assertIn("page_head", entries, "add a part/title-meta-* entry")
        self.assertEqual([o for o in options if not any(f"{o}=" in tag for tag in opened)], [], "draw each option")
        self.assertTrue(any("data_" in tag for tag in opened), "draw a data_ option")
