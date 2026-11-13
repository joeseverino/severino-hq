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
    def test_its_parts_run_as_one_line(self):
        html = _render("{% title_meta %}Online{% dot %}<code>{{ host }}</code>{% endtitle_meta %}", host="example-host")

        (meta,) = with_class(html, "page-title-meta")
        self.assertEqual(meta.tag, "p")
        (code,) = elements(html, "code")
        self.assertIn(meta, code.ancestors)
        self.assertEqual(code.words, "example-host")

    def test_as_a_list_its_items_are_the_pages_with_the_pages_hooks(self):
        html = _render(
            '{% title_meta element="ul" data_record_status=state %}<li>Current</li><li>Read now</li>{% endtitle_meta %}',
            state="current",
        )

        (meta,) = with_class(html, "page-title-meta")
        self.assertEqual(meta.tag, "ul")
        self.assertIn("dot-list", meta.classes)
        self.assertEqual(meta.attrs["data-record-status"], "current")
        self.assertEqual([item.words for item in elements(html, "li")], ["Current", "Read now"])

    def test_another_element_or_option_is_refused(self):
        with self.assertRaises(TemplateSyntaxError):
            _render('{% title_meta element="div" %}A{% endtitle_meta %}')
        with self.assertRaises(TemplateSyntaxError):
            Template('{% title_meta css="example" %}A{% endtitle_meta %}')


@tag("source")
class TitleMetaCatalogTests(SimpleTestCase):
    def test_every_element_and_option_of_title_meta_has_a_catalog_entry(self):
        """The browser survey reads the part through the catalog, so each
        element it draws and each option it takes is drawn there at least
        once. An entry draws it as a page head's aside, so its tags are read
        from the part family's source."""

        from hq.platform.core.templatetags.title_tags import ELEMENTS, register
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
        self.assertEqual(
            sorted(e for e in ELEMENTS - {"p"} if not any(f'element="{e}"' in tag for tag in opened)), [], "draw it"
        )
        self.assertTrue(any("element=" not in tag for tag in opened), "draw the line")
        self.assertTrue(any("data_" in tag for tag in opened), "draw a data_ option")
