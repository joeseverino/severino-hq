"""A command's control is derived from the command: its words, its address,
how it is pressed, whether it looks destructive, and who is offered it."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import path, reverse
from django.views import View

from hq.platform.application import request_context, roles
from hq.platform.application.capabilities import capability_words
from hq.platform.application.lookup import LOOK_UP_NAME
from hq.platform.application.pages import PageAction
from hq.platform.application.resource_capabilities import REMOVAL_CAPABILITY, UPDATE_CAPABILITY
from hq.platform.core.test_runner import no_default_role

RECONCILE = "infrastructure.resource.reconcile"
# The reconcile's mode for a record that only reports on itself.
READS_AGAIN = "reads_again"


def _person(username: str, role: str):
    with no_default_role():
        user = get_user_model().objects.create_user(username)
    if role:
        roles.assign(user, role)
    return user


class CommandControlTests(TestCase):
    def offered_to(self, user, *args, **kwargs) -> PageAction | None:
        request = RequestFactory().get("/")
        request.user = user
        with request_context.bind(request):
            return PageAction.command(*args, **kwargs)

    def test_a_person_who_may_run_it_is_offered_it_in_its_words_at_its_view_s_route(self):
        action = self.offered_to(_person("operator", roles.Role.OPERATOR), UPDATE_CAPABILITY, "example-key")

        assert action is not None
        self.assertEqual(
            (action.label, action.url, action.method, action.danger, action.disabled),
            (
                capability_words(UPDATE_CAPABILITY),
                reverse("control_plane:edit", args=["example-key"]),
                "get",
                False,
                False,
            ),
        )

    def test_a_person_who_may_not_run_it_is_offered_nothing(self):
        viewer = _person("viewer", roles.Role.VIEWER)

        self.assertIsNone(self.offered_to(viewer, UPDATE_CAPABILITY, "example-key"))
        self.assertIsNone(self.offered_to(_person("nobody", ""), REMOVAL_CAPABILITY, "example-key"))

    def test_a_destructive_command_looks_destructive_and_opens_its_confirmation(self):
        action = self.offered_to(_person("operator", roles.Role.OPERATOR), REMOVAL_CAPABILITY, "example-key")

        assert action is not None
        self.assertTrue(action.danger)
        self.assertEqual((action.method, action.url), ("get", reverse("control_plane:remove", args=["example-key"])))

    def test_a_view_that_answers_only_a_post_is_posted_to_in_the_variant_s_words(self):
        action = self.offered_to(
            _person("operator", roles.Role.OPERATOR), RECONCILE, "example-key", variant=READS_AGAIN
        )

        assert action is not None
        self.assertEqual(
            (action.label, action.method, action.url),
            (
                capability_words(RECONCILE, READS_AGAIN),
                "post",
                reverse("control_plane:reconcile", args=["example-key"]),
            ),
        )

    def test_a_command_no_view_runs_is_reached_at_its_own_page(self):
        action = self.offered_to(_person("operator", roles.Role.OPERATOR), LOOK_UP_NAME, query={"name": "a b"})

        assert action is not None
        self.assertEqual(action.url, f"{reverse('command', kwargs={'name': LOOK_UP_NAME})}?name=a+b")

    def test_a_press_that_sends_something_posts_it_but_never_its_button_s_own_field(self):
        operator = _person("operator", roles.Role.OPERATOR)

        sent = self.offered_to(operator, UPDATE_CAPABILITY, "example-key", fields={"next": "/"})
        assert sent is not None
        self.assertEqual(sent.method, "post")
        with self.assertRaises(ValueError):
            self.offered_to(operator, UPDATE_CAPABILITY, "example-key", field="next", fields={"next": "/"})


class HandBuiltControlTests(SimpleTestCase):
    def test_a_hand_built_control_cannot_post_look_destructive_or_send_anything(self):
        for given in ({"method": "post"}, {"danger": True}, {"value": "1"}, {"fields": {"id": "1"}}):
            with self.subTest(given), self.assertRaises(TypeError):
                PageAction("Example", "/example/", **given)  # type: ignore[call-arg]

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

        with self.assertRaisesMessage(ImproperlyConfigured, "'one' and at 'two'"):
            command_route(UPDATE_CAPABILITY)
