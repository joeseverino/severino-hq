"""Markup a page would otherwise write by hand is drawn by its one partial or
tag, so it reads the same everywhere and a change to it is made once."""

from pathlib import Path

from django import forms
from django.conf import settings
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag

from .markup import select, with_class

TEMPLATES = Path(settings.BASE_DIR) / "templates"


def _templates():
    """Each template's name and source, the one walk every guard here reads."""

    for path in sorted(TEMPLATES.rglob("*.html")):
        yield path.relative_to(TEMPLATES).as_posix(), path.read_text(encoding="utf-8")


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


@tag("source")
class SharedMarkupTemplateTests(SimpleTestCase):
    """No template writes by hand what a shared partial or tag draws."""

    def test_only_the_partial_prints_a_form_s_own_errors(self):
        found = [name for name, text in _templates() if name != FormErrorsTests.PARTIAL and ".non_field_errors" in text]
        self.assertEqual(found, [], f'include "{FormErrorsTests.PARTIAL}" instead')
