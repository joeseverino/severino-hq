"""A page prints only text and numbers, whatever HQ or extension template prints the value."""

from datetime import UTC, date, datetime, time, timedelta
from enum import Enum
from pathlib import Path

from django import template
from django.conf import settings
from django.contrib.auth import get_user_model
from django.template import Context, Engine, Template
from django.template.base import Origin
from django.test import SimpleTestCase, override_settings

from hq.platform.application.printable import Unprintable
from hq.platform.application.ui import MISSING


class Plain(Enum):
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


class PrintGateTests(SimpleTestCase):
    def test_a_template_printing_a_refused_value_fails(self):
        for what, value in REFUSED.items():
            with self.subTest(what), self.assertRaises(Unprintable):
                printed("<p>{{ value }}</p>", value=value)

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

    @override_settings(SEVERINO_STRICT_PRINT=False)
    def test_in_production_a_refusal_prints_the_missing_mark_and_logs_where_never_what(self):
        with self.assertLogs("severino.print", "ERROR") as logged:
            rendered = printed('<b title="{{ value }}">{{ value }}</b>', value=Credential())

        self.assertNotIn(repr(Credential()), rendered)
        self.assertIn(MISSING, rendered)
        self.assertIn("example/page.html:1", logged.output[0])
        self.assertIn(Credential.__qualname__, logged.output[0])
        self.assertNotIn(repr(Credential()), "".join(logged.output))
