"""A command's control is derived from the command: its words, its address,
how it is pressed, whether it looks destructive, and who is offered it."""

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path, reverse
from django.views import View

from hq.platform.application.lookup import LOOK_UP_NAME
from hq.platform.application.pages import PageAction
from hq.platform.application.resource_capabilities import UPDATE_CAPABILITY


class HandBuiltControlTests(SimpleTestCase):
    def test_a_link_to_a_page_that_runs_a_command_is_refused(self):
        for url in (
            reverse("control_plane:edit", args=["example-key"]),
            reverse("command", kwargs={"name": LOOK_UP_NAME}),
        ):
            with self.subTest(url), self.assertRaises(ImproperlyConfigured):
                PageAction("Example", url)


class _Runs(View):
    runs = (UPDATE_CAPABILITY,)

    def post(self, request):
        return HttpResponse()


urlpatterns = [
    path("one/", _Runs.as_view(), name="one"),
    path("two/", _Runs.as_view(), name="two"),
]


@override_settings(ROOT_URLCONF=__name__)
class CommandRouteTests(SimpleTestCase):
    def test_two_routes_that_run_one_command_are_refused(self):
        from hq.platform.application.pages import command_route

        with self.assertRaises(ImproperlyConfigured):
            command_route(UPDATE_CAPABILITY)
