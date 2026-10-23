"""A page prints only text and numbers, whatever HQ or extension template prints the value."""

import ast
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import Enum, IntEnum, StrEnum
from pathlib import Path, PurePosixPath
from unittest import mock

import django
from django import forms, template
from django.apps import apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Engine, Template, base, engines
from django.template.base import Origin
from django.test import SimpleTestCase, override_settings
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy

from hq.platform.application.printable import Unprintable
from hq.platform.application.ui import MISSING
from hq.platform.core import print_gate
from hq.platform.core.templatetags.value_tags import breakable, or_empty, readable, readout


class Plain(Enum):
    ONE = 1


class Worded(StrEnum):
    ONE = "one"


class Counted(IntEnum):
    ONE = 1


class Credential:
    def __repr__(self):
        return "example-secret"


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
}

ALLOWED = {
    "text": "example-web",
    # A note, a title or a release tag can be typed as a date: it is text.
    "text that is a date": "2026-10-03",
    "text that is an instant": "2026-10-03T09:29:15+00:00",
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


def ours(name):
    """Where HQ's own template ``name`` is read from."""

    return Origin(str(Path(settings.BASE_DIR) / "templates" / name), template_name=name)


register = template.Library()


@register.simple_tag
def example_value(value):
    return value


@register.inclusion_tag(Template("{{ value }}", origin=ours("example/part.html")))
def example_part(value):
    return {"value": value}


ENGINE = Engine(libraries={"i18n": "django.templatetags.i18n", "example_print": __name__})


def printed(source, origin=None, **context):
    page = Template(source, origin=origin or ours("example/page.html"), engine=ENGINE)
    return page.render(Context(context))


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
        parent = Template("<main>{% block body %}{% endblock %}</main>", origin=ours("example/parent.html"))

        with self.assertRaisesMessage(Unprintable, "example/child.html:3 printed a flag"):
            printed(
                "{% extends parent %}\n{% block body %}\n{{ value }}{% endblock %}",
                origin=ours("example/child.html"),
                parent=parent,
                value=True,
            )

    def test_every_tag_that_prints_is_held(self):
        part = Template("{{ value }}", origin=ours("example/part.html"))
        for source in (
            "{% firstof value %}",
            "{% for x in xs %}{% cycle value 'b' %}{% endfor %}",
            "{% load i18n %}{% translate value noop %}",
            "{% load i18n %}{% blocktranslate %}{{ value }}{% endblocktranslate %}",
            "{% include part %}",
            "{% load example_print %}{% example_value value %}",
            "{% load example_print %}{% example_part value %}",
        ):
            with self.subTest(source), self.assertRaises(Unprintable):
                printed(source, value=Plain.ONE, xs=[1], part=part)

    def test_a_simple_tag_result_kept_in_a_variable_is_not_printed(self):
        self.assertEqual(printed("{% load example_print %}{% example_value value as kept %}", value=True), "")

    def test_the_filters_that_make_text_refuse_what_the_gate_refuses(self):
        for name, write in {
            "breakable": breakable,
            "or_empty": or_empty,
            "readable": readable,
            "readout": readout,
        }.items():
            with self.subTest(name), self.assertRaises(Unprintable):
                write(True)

    @override_settings(SEVERINO_STRICT_PRINT=False)
    def test_in_production_a_refusal_prints_the_missing_mark_and_logs_where_never_what(self):
        with self.assertLogs("severino.print", "ERROR") as logged:
            rendered = printed('<b title="{{ value }}">{{ value }}</b>', value=Credential())

        self.assertEqual(rendered, f'<b title="{MISSING}">{MISSING}</b>')
        self.assertIn("example/page.html:1", logged.output[0])
        self.assertIn(Credential.__qualname__, logged.output[0])
        self.assertNotIn(repr(Credential()), "".join(logged.output))

    def test_a_print_with_no_template_behind_it_is_left_as_django_prints_it(self):
        self.assertEqual(base.render_value_in_context(True, Context()), "True")
        unplaced = base.VariableNode(base.FilterExpression("value", base.Parser([])))
        self.assertEqual(unplaced.render(Context({"value": True})), "True")

    def test_installing_twice_gates_once(self):
        printing = base.render_value_in_context
        renders = [node.render for node in print_gate.PRINTING]

        print_gate.install()

        self.assertIs(base.render_value_in_context, printing)
        self.assertEqual([node.render for node in print_gate.PRINTING], renders)


class ScopeTests(SimpleTestCase):
    def setUp(self):
        print_gate._ours.cache_clear()
        self.addCleanup(print_gate._ours.cache_clear)

    def test_an_extension_app_template_is_held_and_a_third_party_one_is_not(self):
        app = apps.get_app_config("mozilla_django_oidc")
        origin = Origin(str(Path(app.path) / "templates" / "example.html"), template_name="example.html")

        self.assertEqual(printed("{{ value }}", origin=origin, value=True), "True")
        print_gate._ours.cache_clear()
        with (
            mock.patch("hq.platform.core.print_gate.installed_plugin_apps", return_value=[app.name]),
            self.assertRaises(Unprintable),
        ):
            printed("{{ value }}", origin=origin, value=True)

    def test_a_template_built_from_a_string_is_not_held(self):
        self.assertEqual(engines["django"].from_string("{{ value }}").render({"value": True}), "True")

    def test_django_s_own_widgets_print_a_stamp_and_a_file(self):
        stored = SimpleUploadedFile("example-receipt.pdf", b"x")
        stored.url = "/files/example-receipt.pdf"
        form = _Form(initial={"on": date(2026, 10, 3), "file": stored})

        rendered = printed("{{ form }}", form=form)

        self.assertIn('value="2026-10-03"', rendered)
        self.assertIn("example-receipt.pdf", rendered)


def _printers(path: Path, names: set[str]) -> set[str]:
    """The top-level classes and functions in ``path`` that call one of ``names``."""

    return {
        statement.name
        for statement in ast.parse(path.read_text()).body
        if isinstance(statement, (ast.ClassDef, ast.FunctionDef))
        and any(
            isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in names
            for call in ast.walk(statement)
        )
    }


class DjangoPrintersTests(SimpleTestCase):
    def test_the_gate_covers_every_node_the_installed_django_prints_through(self):
        """A Django upgrade that prints through ``render_value_in_context``
        somewhere new fails here until ``PRINTING`` names it."""

        installed = Path(django.__file__).parent
        found = {
            f"{'.'.join(path.relative_to(installed.parent).with_suffix('').parts)}.{name}"
            for path in (*installed.glob("template/**/*.py"), *installed.glob("templatetags/*.py"))
            for name in _printers(path, {"render_value_in_context"})
        }
        found |= {
            f"django.template.library.{name}"
            for name in _printers(installed / "template" / "library.py", {"conditional_escape"})
        }

        self.assertEqual(found, {f"{node.__module__}.{node.__qualname__}" for node in print_gate.PRINTING})
