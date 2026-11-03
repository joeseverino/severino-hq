"""What a caller who has not signed in can make HQ write: every route and middleware that serves one says."""

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path
from django.utils.module_loading import import_string

from hq.platform.core import speculation
from hq.platform.core.write_bounds import NOTHING, Bound, declared, writes


def undeclared_routes() -> list[str]:
    """Each route answering without a session whose view declares nothing it writes."""

    return sorted(route for route, view in speculation.sessionless_views() if not declared(view))


def undeclared_middleware() -> list[str]:
    """Each of HQ's middleware, which runs for a caller without a session too, that declares nothing."""

    return [name for name in settings.MIDDLEWARE if name.startswith("hq.") and not declared(import_string(name))]


class DeclaredTests(SimpleTestCase):
    def test_every_route_that_answers_without_a_session_declares_what_it_writes(self):
        self.assertEqual(
            undeclared_routes(),
            [],
            "A route that answers without a session declares what it writes where it is defined: "
            "@writes(NOTHING), or @writes(<the Bound that holds it>) from hq.platform.core.write_bounds.",
        )

    def test_every_middleware_of_hqs_declares_what_it_writes(self):
        self.assertEqual(
            undeclared_middleware(),
            [],
            "A middleware of HQ's runs before any sign-in check, so it declares what it writes with @writes.",
        )

    def test_a_new_route_that_declares_nothing_is_found(self):
        with override_settings(ROOT_URLCONF=__name__):
            self.assertEqual(undeclared_routes(), ["/example/undeclared/"])

    def test_a_declaration_names_at_least_one_bound(self):
        for bounds in ((), ("a sentence",)):
            with self.subTest(bounds=bounds), self.assertRaises(TypeError):
                writes(*bounds)


@login_not_required
def _undeclared(_request):
    return HttpResponse()


@writes(NOTHING)
@login_not_required
def _quiet(_request):
    return HttpResponse()


@writes(Bound("example", "one row, ever"))
@login_not_required
def _bounded(_request):
    return HttpResponse()


def _signed_in(_request):
    return HttpResponse()


urlpatterns = [
    path("example/undeclared/", _undeclared),
    path("example/quiet/", _quiet),
    path("example/bounded/", _bounded),
    path("example/signed-in/", _signed_in),
]
