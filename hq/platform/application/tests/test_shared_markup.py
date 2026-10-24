"""Markup a page would otherwise write by hand is drawn by its one partial or
tag, so it reads the same everywhere and a change to it is made once."""

import re
from pathlib import Path

from django import forms
from django.conf import settings
from django.template import Context, Template, TemplateSyntaxError
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from .markup import elements, select, with_class

TEMPLATES = Path(settings.BASE_DIR) / "templates"


def _templates():
    """Each template's name and source, the one walk every guard here reads."""

    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")


def _written_by_hand(pattern: str, partial: str) -> list[str]:
    """Every template but ``partial`` that writes ``pattern``."""

    return [name for name, text in _templates() if name != partial and re.search(pattern, text)]


def _render(source: str, **context) -> str:
    return Template(source).render(Context(context))


class _Refused(forms.Form):
    name = forms.CharField(required=False)

    def clean(self):
        raise forms.ValidationError("Those two cannot go together.")


class FormErrorsTests(SimpleTestCase):
    PARTIAL = "partials/_form_errors.html"

    def test_a_form_s_own_errors_are_announced_above_its_fields(self):
        form = _Refused(data={"name": "x"})
        form.is_valid()

        (notice,) = with_class(render_to_string(self.PARTIAL, {"form": form}), "msg-error")

        self.assertEqual(notice.attrs.get("role"), "alert")
        self.assertIn("Those two cannot go together.", notice.words)

    def test_a_form_without_them_draws_nothing(self):
        form = forms.Form(data={})
        form.is_valid()

        self.assertEqual(select(render_to_string(self.PARTIAL, {"form": form}), "role"), [])


class SectionHeadTests(SimpleTestCase):
    PARTIAL = "partials/_section_head.html"

    def test_the_heading_is_named_and_what_sits_beside_it_follows(self):
        html = _render(
            '{% section_head id="example-heading" %}Hosts <span class="count">{{ n }}</span>'
            '{% aside %}<a href="/example/">All</a>{% endsection_head %}',
            n=3,
        )

        (head,) = with_class(html, "section-head")
        (heading,) = elements(html, "h2")
        (link,) = elements(html, "a")
        self.assertEqual(heading.attrs["id"], "example-heading")
        self.assertEqual(heading.words, "Hosts 3")
        self.assertIn(head, heading.ancestors)
        self.assertNotIn(heading, link.ancestors)
        self.assertIn(head, link.ancestors)

    def test_a_lede_is_grouped_with_its_heading(self):
        html = _render("{% section_head lede=lede %}Load{% endsection_head %}", lede="Read a minute ago")

        (group,) = elements(html, "hgroup")
        (line,) = with_class(html, "section-lede")
        self.assertIn(group, line.ancestors)
        self.assertEqual(line.words, "Read a minute ago")

    def test_an_empty_lede_leaves_the_heading_alone(self):
        self.assertEqual(
            elements(_render("{% section_head lede=lede %}Load{% endsection_head %}", lede=""), "hgroup"), []
        )

    def test_what_the_page_writes_is_escaped_as_it_always_is(self):
        html = _render("{% section_head %}{{ name }}{% endsection_head %}", name="<b>x</b>")

        self.assertEqual(elements(html, "b"), [])

    def test_an_option_it_does_not_take_is_refused(self):
        for source in (
            '{% section_head class="x" %}A{% endsection_head %}',
            '{% section_head "A" %}A{% endsection_head %}',
        ):
            with self.subTest(source=source), self.assertRaises(TemplateSyntaxError):
                Template(source)


@tag("source")
class SharedMarkupTemplateTests(SimpleTestCase):
    """No template writes by hand what a shared partial or tag draws."""

    def test_only_the_partial_prints_a_form_s_own_errors(self):
        found = _written_by_hand(r"\.non_field_errors", FormErrorsTests.PARTIAL)
        self.assertEqual(found, [], f'include "{FormErrorsTests.PARTIAL}" instead')

    def test_only_the_tag_draws_a_section_head(self):
        found = _written_by_hand(r'class="section-head[" ]', SectionHeadTests.PARTIAL)
        self.assertEqual(found, [], "write {% section_head %} instead")
