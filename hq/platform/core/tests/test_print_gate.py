"""A page prints only text and numbers, whatever template prints the value."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import PurePosixPath

from django import forms
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Engine, engines
from django.test import SimpleTestCase, override_settings
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from hq.platform.application.ui import MISSING
from hq.platform.core.print_gate import Unprintable
from hq.platform.core.templatetags.value_tags import breakable, or_empty, readable, readout


class Plain(Enum):
    ONE = 1


class Worded(StrEnum):
    ONE = "one"


class Counted(IntEnum):
    ONE = 1


REFUSED = {
    "a flag": True,
    "None": None,
    "a date": date(2026, 10, 3),
    "a datetime": datetime(2026, 10, 3, 9, 29, tzinfo=UTC),
    "a time": time(9, 29),
    "a length of time": timedelta(days=1),
    "a mapping": {"a": 1},
    "a list": ["a"],
    "a tuple": ("a",),
    "a set": {"a"},
    "bytes": b"a",
    "a plain enum member": Plain.ONE,
    "a model instance": get_user_model()(username="example-operator"),
    "an object": object(),
    "an ISO date": "2026-10-03",
    "an ISO instant": "2026-10-03T09:29:15+00:00",
}

ALLOWED = {
    "text": "example-web",
    "text holding a date": "due 2026-10-03",
    "a figure that is a basic-form date": "20261003",
    "a whole number": 3,
    "a fraction": 2.5,
    "a decimal": Decimal("1.50"),
    "zero": 0,
    "markup": mark_safe("<b>x</b>"),
    "a lazy translation": gettext_lazy("Name"),
    "a str enum member": Worded.ONE,
    "an int enum member": Counted.ONE,
    "a UUID": uuid.UUID(int=1),
    "a path": PurePosixPath("/srv/example"),
}


def printed(source, **context):
    return engines["django"].from_string(source).render(context)


class _Form(forms.Form):
    on = forms.DateField(widget=forms.DateInput(attrs={"type": "date"}))
    file = forms.FileField(required=False)


class PrintGateTests(SimpleTestCase):
    def test_a_template_printing_a_refused_value_fails(self):
        for what, value in REFUSED.items():
            with self.subTest(what), self.assertRaises(Unprintable):
                printed("<p>{{ value }}</p>", value=value)

    def test_a_template_prints_text_and_numbers(self):
        for what, value in ALLOWED.items():
            with self.subTest(what):
                printed("<p>{{ value }}</p>", value=value)

    def test_a_refusal_names_the_template_and_line_that_printed_it(self):
        engine = Engine(
            loaders=[
                (
                    "django.template.loaders.locmem.Loader",
                    {
                        "example/parent.html": "<main>{% block body %}{% endblock %}</main>",
                        "example/child.html": '{% extends "example/parent.html" %}\n{% block body %}\n{{ value }}{% endblock %}',
                    },
                )
            ]
        )

        with self.assertRaisesMessage(Unprintable, "example/child.html:3 printed a flag"):
            engine.get_template("example/child.html").render(Context({"value": True}))

    def test_firstof_and_cycle_are_held_too(self):
        with self.assertRaises(Unprintable):
            printed("{% firstof value %}", value=True)
        with self.assertRaises(Unprintable):
            printed("{% for x in xs %}{% cycle value 'b' %}{% endfor %}", xs=[1], value=None)

    def test_the_filters_that_make_text_refuse_what_the_gate_refuses(self):
        for name, write in {
            "breakable": breakable,
            "or_empty": or_empty,
            "readable": readable,
            "readout": readout,
        }.items():
            with self.subTest(name), self.assertRaises(Unprintable):
                write(True)

    def test_django_s_own_widgets_print_a_stamp_and_a_file(self):
        stored = SimpleUploadedFile("example-receipt.pdf", b"x")
        stored.url = "/files/example-receipt.pdf"
        form = _Form(initial={"on": date(2026, 10, 3), "file": stored})

        rendered = printed("{{ form }}", form=form)

        self.assertIn('value="2026-10-03"', rendered)
        self.assertIn("example-receipt.pdf", rendered)

    @override_settings(SEVERINO_STRICT_PRINT=False)
    def test_in_production_a_refusal_prints_the_missing_mark_and_is_logged(self):
        with self.assertLogs("severino.print", "ERROR") as logged:
            rendered = printed('<time datetime="{{ value }}">{{ value }}</time>', value="2026-10-03")

        self.assertEqual(rendered, f'<time datetime="{MISSING}">{MISSING}</time>')
        self.assertIn("ISO 8601", logged.output[0])
