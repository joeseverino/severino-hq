"""A command's control is derived from the command: its words, its address,
how it is pressed, whether it looks destructive, and who is offered it."""

from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path, reverse
from django.views import View

from hq.platform.application import request_context, roles
from hq.platform.application.lookup import LOOK_UP_NAME
from hq.platform.application.pages import PageAction, PageDialog
from hq.platform.application.resource_capabilities import UPDATE_CAPABILITY
from hq.platform.core.testing import person

RECONCILE = "infrastructure.resource.reconcile"
# The reconcile's mode for a record that only reports on itself.
READS_AGAIN = "reads_again"


def _person(username: str, role: str):
    user = person(username=username)
    if role:
        roles.assign(user, role)
    return user


class CommandControlTests(TestCase):
    def offered_to(self, user, *args, **kwargs) -> PageAction | None:
        request = RequestFactory().get("/")
        request.user = user
        with request_context.bind(request):
            return PageAction.command(*args, **kwargs)

    def test_a_press_that_sends_something_posts_it_but_never_its_button_s_own_field(self):
        operator = _person("operator", roles.Role.OPERATOR)

        sent = self.offered_to(operator, UPDATE_CAPABILITY, "example-key", fields={"next": "/"})
        assert sent is not None
        self.assertEqual(sent.method, "post")
        with self.assertRaises(ValueError):
            self.offered_to(operator, UPDATE_CAPABILITY, "example-key", field="next", fields={"next": "/"})

    def test_a_command_whose_view_draws_a_form_may_open_it_in_a_dialog_and_one_that_posts_may_not(self):
        operator = _person("operator", roles.Role.OPERATOR)
        dialog = PageDialog("example-edit", "Edit", "example/_edit_form.html")

        opened = self.offered_to(operator, UPDATE_CAPABILITY, "example-key", modal=dialog)
        assert opened is not None
        self.assertEqual((opened.method, opened.dialog), ("get", dialog))
        with self.assertRaises(ValueError):
            self.offered_to(operator, RECONCILE, "example-key", variant=READS_AGAIN, modal=dialog)


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
