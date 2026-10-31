"""A template shows a moment through ``when``: Django's own ways to write one
are not in HQ's engine."""

from django.template import Context, Template, TemplateSyntaxError, engines
from django.test import SimpleTestCase
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
