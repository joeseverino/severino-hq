"""A template shows a moment through ``when``: Django's own ways to write one
are not in HQ's engine."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.template import Context, Template, TemplateSyntaxError, engines
from django.test import SimpleTestCase, override_settings
from django.views.debug import DEBUG_ENGINE


class WithheldTests(SimpleTestCase):
    def test_a_template_that_writes_a_moment_itself_fails_as_it_compiles(self):
        for source in (
            '{{ on|date:"Y-m-d" }}',
            "{{ on|date }}",
            "{{ on|time }}",
            "{{ on|timesince }}",
            "{{ on|timeuntil }}",
            '{% now "Y" %}',
            "{{ n|pluralize }}",
            "{{ n|filesizeformat }}",
            "{% load humanize %}{{ on|naturaltime }}",
        ):
            with self.subTest(source), self.assertRaises(TemplateSyntaxError):
                engines["django"].from_string(source)

    def test_a_template_built_without_an_engine_is_held_too(self):
        with self.assertRaises(TemplateSyntaxError):
            Template('{{ on|date:"Y" }}')

    def test_the_rest_of_django_s_builtins_are_there(self):
        rendered = Template("{% if names %}{{ names|first|upper }}{% endif %}").render(Context({"names": ["example"]}))

        self.assertEqual(rendered, "EXAMPLE")

    def test_another_engine_keeps_django_s_whole_library(self):
        """Django draws its own error page with an engine of its own, and it prints a date."""

        for source in ('{{ on|date:"Y" }}', '{% now "Y" %}'):
            with self.subTest(source):
                DEBUG_ENGINE.from_string(source)


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
        with self.assertRaisesMessage(TemplateSyntaxError, "example/page.html:2: A fold drawn by hand"):
            self.loaded("<p>Example</p>\n<DETAILS open><summary>More</summary></DETAILS>")

    def test_a_template_comment_holds_none(self):
        self.loaded("{% comment %}<details>{% endcomment %}{# <details> #}<p>Example</p>")

    def test_a_template_that_draws_the_shape_itself_loads(self):
        self.loaded("<details><summary>Example</summary></details>", "partials/_check_rung.html")

    def test_each_shape_refuses_its_examples_and_keeps_the_rest(self):
        from hq.platform.core.hand_drawn import SHAPES, Source

        for shape in SHAPES:
            for source in shape.refuses:
                with self.subTest(shape.name, refused=source):
                    self.assertTrue(list(shape.find(Source(source))))
            for source in shape.keeps:
                with self.subTest(shape.name, kept=source):
                    self.assertEqual(list(shape.find(Source(source))), [])
