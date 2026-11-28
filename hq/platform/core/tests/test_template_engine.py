"""What HQ's engine refuses as a template loads: a shape a shared part
draws, written by hand, and a literal a filter or a tag refuses."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.template import TemplateSyntaxError, engines
from django.test import SimpleTestCase, override_settings, tag


def loaded(source: str, name: str = "example/page.html", **beside: str):
    """The template ``name`` written as ``source``, with each template
    ``beside`` it (``part="..."`` is ``example/part.html``), as the engine loads it."""

    with tempfile.TemporaryDirectory() as directory:
        for written, text in {name: source, **{f"example/{key}.html": text for key, text in beside.items()}}.items():
            Path(directory, written).parent.mkdir(parents=True, exist_ok=True)
            Path(directory, written).write_text(text, encoding="utf-8")
        backend = settings.TEMPLATES[0] | {"DIRS": [directory]}
        with override_settings(TEMPLATES=[backend]):
            return engines["django"].get_template(name)


class DrawnTests(SimpleTestCase):
    """A shape a shared part draws, written by hand, fails as its template loads."""

    def test_a_shape_drawn_by_hand_fails_as_its_template_loads(self):
        with self.assertRaisesMessage(TemplateSyntaxError, "example/page.html:2"):
            loaded("<p>Example</p>\n<DETAILS open><summary>More</summary></DETAILS>")

    def test_each_shape_refuses_its_examples_and_keeps_the_rest(self):
        from hq.platform.core.hand_drawn import SHAPES
        from hq.platform.core.template_source import Source

        for shape in SHAPES:
            for source in shape.refuses:
                with self.subTest(shape.name, refused=source):
                    self.assertTrue(list(shape.find(Source(source))))
            for source in shape.keeps:
                with self.subTest(shape.name, kept=source):
                    self.assertEqual(list(shape.find(Source(source))), [])


class LiteralTests(SimpleTestCase):
    """A literal a filter or a tag refuses, of a value or of values together,
    fails as its template loads, written there or handed over by an include;
    one it takes loads."""

    def test_a_literal_refused_fails_where_it_is_written(self):
        # A filter's names the line too.
        for source, where in (
            ('{{ n|counted:"newer release" }}', ":2"),
            ('{% include "example/part.html" with phrase="newer release" %}', ":2"),
            ('{% status "Up" tone="goood" %}', ""),
            ('{% status "Up" chip=True tone="good" %}', ""),
            ('{% button "Open" "/example/" icon="hq-no-such-icon" %}', ""),
            ('{% button "Example" "/example/" look="ghost" css="example-pin" %}', ""),
            ('{% table_heading "Name" sort="name" local_sort=True %}', ""),
        ):
            with self.subTest(source), self.assertRaisesMessage(TemplateSyntaxError, f"example/page.html{where}"):
                loaded(f"<p>Example</p>\n{source}", part="{{ n|counted:phrase }}")

    def test_a_literal_a_filter_takes_loads(self):
        page = '{{ n|counted:"newer release,newer releases" }}{% include "example/part.html" with phrase="row" %}'
        loaded(page, part="{{ n|counted:phrase }}")


@tag("tree")
class EveryTemplateTests(SimpleTestCase):
    """Every template HQ and its extensions serve compiles, so one no page
    walk draws is refused before it ships."""

    def test_every_template_compiles(self):
        from hq.platform.application.tests.source import template_files

        for name, _path in template_files():
            with self.subTest(name):
                engines["django"].get_template(name)
