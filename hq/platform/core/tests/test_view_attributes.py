"""No view defines an attribute Django's generic views read for themselves.

A class attribute, property or cached_property that shares a name with one
Django's view machinery reads silently replaces it: a `content_type` property
on a form view became the response's `Content-Type` header. The reserved set
is read off the installed Django's generic view modules, minus the names a
subclass is meant to set.
"""

import ast
import importlib
import inspect
from pathlib import Path

from django.test import SimpleTestCase, tag
from django.views.generic import base, dates, detail, edit, list as listing

import hq

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


def view_like(node: ast.ClassDef) -> bool:
    bases = (b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", "") for b in node.bases)
    return any(name.endswith(("View", "Mixin")) for name in bases)


def view_classes(root: Path) -> list[type]:
    classes = []
    for path in sorted(root.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "View" not in source and "Mixin" not in source:
            continue
        found = [n.name for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef) and view_like(n)]
        if not found:
            continue
        module = importlib.import_module(".".join(("hq", *path.relative_to(root).with_suffix("").parts)))
        classes += [getattr(module, name) for name in found if isinstance(getattr(module, name, None), type)]
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
