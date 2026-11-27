"""A template shows a moment through ``when``: Django's own ways to write one
are not in HQ's engine."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.template import TemplateSyntaxError, engines
from django.test import SimpleTestCase, override_settings


class DrawnTests(SimpleTestCase):
    """A shape a shared part draws, written by hand, fails as its template loads."""

    def loaded(self, source: str, name: str = "example/page.html"):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, name).parent.mkdir(parents=True, exist_ok=True)
            Path(directory, name).write_text(source, encoding="utf-8")
            backend = settings.TEMPLATES[0] | {"DIRS": [directory]}
            with override_settings(TEMPLATES=[backend]):
                return engines["django"].get_template(name)

    def test_a_shape_drawn_by_hand_fails_as_its_template_loads(self):
        with self.assertRaisesMessage(TemplateSyntaxError, "example/page.html:2"):
            self.loaded("<p>Example</p>\n<DETAILS open><summary>More</summary></DETAILS>")

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
