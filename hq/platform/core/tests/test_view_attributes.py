"""No view defines an attribute Django's generic views read for themselves.

A class attribute, property or cached_property that shares a name with one
Django's view machinery reads silently replaces it: a `content_type` property
on a form view became the response's `Content-Type` header. The reserved set
is read off the installed Django's generic view modules, minus the names a
subclass is meant to set.
"""

import importlib
import inspect
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.views.generic import base, dates, detail, edit, list as listing

import hq
from hq.platform.application.tests.facts import module_name, scan

# Attributes Django documents as a subclass's to set, each why it is safe.
MEANT_TO_SET = {
    "allow_empty": "list configuration",
    "allow_future": "date view configuration",
    "context_object_name": "names the object in the template context",
    "date_field": "date view configuration",
    "date_list_period": "date view configuration",
    "day": "date view configuration",
    "day_format": "date view configuration",
    "extra_context": "documented way to add template context",
    "fields": "model form configuration",
    "form_class": "the form a view builds",
    "http_method_names": "the methods a view answers",
    "initial": "documented form initial values",
    "make_object_list": "date view configuration",
    "model": "the model a view reads",
    "month": "date view configuration",
    "month_format": "date view configuration",
    "ordering": "list configuration",
    "page_kwarg": "list configuration",
    "paginate_by": "list configuration",
    "paginate_orphans": "list configuration",
    "paginator_class": "list configuration",
    "pattern_name": "redirect configuration",
    "permanent": "redirect configuration",
    "pk_url_kwarg": "object lookup configuration",
    "prefix": "documented form prefix",
    "preserve_request": "redirect configuration",
    "query_pk_and_slug": "object lookup configuration",
    "query_string": "redirect configuration",
    "queryset": "the rows a view reads",
    "slug_field": "object lookup configuration",
    "slug_url_kwarg": "object lookup configuration",
    "success_url": "where a form view goes next",
    "template_engine": "documented template engine choice",
    "template_name": "the template a view renders",
    "template_name_field": "object template choice",
    "template_name_suffix": "object template choice",
    "uses_datetime_field": "date view configuration",
    "url": "redirect configuration",
    "week": "date view configuration",
    "week_format": "date view configuration",
    "year": "date view configuration",
    "year_format": "date view configuration",
}

# State a view's own methods set on the instance.
INSTANCE_STATE = {"request", "args", "kwargs", "object", "object_list", "headers"}


def django_owned() -> set[str]:
    """Every non-method name the generic view modules' classes define."""

    names: set[str] = set()
    for module in (base, detail, edit, listing, dates):
        for value in vars(module).values():
            if issubclass(type(value), type) and value.__module__ == module.__name__:
                names |= {
                    name
                    for name, member in vars(value).items()
                    if not name.startswith("__")
                    and not inspect.isfunction(member)
                    and not isinstance(member, classmethod | staticmethod)
                }
    return names


RESERVED = (django_owned() | INSTANCE_STATE) - MEANT_TO_SET.keys()


# A class with a base named, or reached as an attribute named, ...View or ...Mixin.
VIEW_LIKE = {
    "kind": "class_definition",
    "has": {"field": "name", "pattern": "$NAME"},
    "all": [
        {
            "has": {
                "field": "superclasses",
                "has": {"any": [{"kind": "identifier"}, {"kind": "attribute"}], "regex": r"(View|Mixin)$"},
            }
        }
    ],
}


def view_classes(*paths: Path) -> list[type]:
    """Each view-like class defined at the top of a module under ``paths``."""

    classes = []
    for found in scan({"view-like": VIEW_LIKE}, *paths):
        value = getattr(importlib.import_module(module_name(found.path)), found.captured["NAME"], None)
        if isinstance(value, type):
            classes.append(value)
    return classes


def shadowing(cls: type) -> list[str]:
    return sorted(RESERVED & vars(cls).keys())


@tag("source", "tree")
class ViewAttributeTests(SimpleTestCase):
    def test_no_view_defines_an_attribute_django_reads(self):
        found = [
            f"{cls.__module__}.{cls.__qualname__}.{name}"
            for cls in view_classes(Path(hq.__file__).parent)
            for name in shadowing(cls)
        ]

        self.assertEqual(
            found,
            [],
            "Each name is one Django's generic views read (`content_type` is the response's header): rename it to"
            " what it means",
        )

    def test_the_reserved_set_holds_the_names_that_broke_a_page(self):
        self.assertIn("content_type", RESERVED)
        self.assertIn("object", RESERVED)
        self.assertNotIn("template_name", RESERVED)
