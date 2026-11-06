"""The one walk of the URL configuration."""

from django.http import HttpResponse
from django.test import SimpleTestCase
from django.urls import include, path, re_path
from django.views import View

from ..routes import routed


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
