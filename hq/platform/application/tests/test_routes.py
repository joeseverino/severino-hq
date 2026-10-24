"""The one walk of the URL configuration."""

import ast
from pathlib import Path

from django.http import HttpResponse
from django.test import SimpleTestCase, tag
from django.urls import include, path, re_path
from django.views import View

from ..routes import routed

ROOT = Path(__file__).resolve().parents[4]
WALKER = ROOT / "hq/platform/application/routes.py"


def page(request):
    return HttpResponse()


class Detail(View):
    pass


PATTERNS = [
    path("", page, name="home"),
    path(
        "control/",
        include(
            (
                [
                    path("machines/<str:name>/", Detail.as_view(), name="machine"),
                    re_path(r"^raw/$", page),
                ],
                "control",
            )
        ),
    ),
]


class RoutedTests(SimpleTestCase):
    def test_each_route_has_its_reversible_name_its_path_and_its_view(self):
        found = [(route.name, route.path, route.view) for route in routed(PATTERNS)]

        self.assertEqual(
            found,
            [
                ("home", "/", page),
                ("control:machine", "/control/machines/<str:name>/", Detail),
                ("", "/control/^raw/$", page),
            ],
        )

    def test_the_mounts_are_handed_over_outermost_first(self):
        machine = next(route for route in routed(PATTERNS) if route.name == "control:machine")

        self.assertEqual([mount.namespace for mount in machine.mounts], ["control"])


def _walks(tree: ast.AST) -> list[int]:
    """Lines reading a resolver's ``url_patterns`` other than the root's: a
    walk into a mount."""

    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "url_patterns"
        and not (isinstance(node.value, ast.Call) and getattr(node.value.func, "id", "") == "get_resolver")
    ]


@tag("source", "tree")
class OneWalkTests(SimpleTestCase):
    def test_only_routed_walks_into_a_mount(self):
        """A second walk drifts from the first: one skips a namespace, one
        drops a regex mount, and a page list stops matching the routes."""

        found = []
        for package in ("hq", "hq_sdk"):
            for source in sorted((ROOT / package).rglob("*.py")):
                if source == WALKER:
                    continue
                lines = _walks(ast.parse(source.read_text(), str(source)))
                found.extend(f"{source.relative_to(ROOT)}:{line}" for line in lines)

        self.assertTrue(_walks(ast.parse(WALKER.read_text())), "routed no longer walks: the rule checks nothing")
        self.assertEqual(found, [])
