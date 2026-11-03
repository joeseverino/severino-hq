"""HQ offers a link to one of its pages only to a person who may open it.

The nav, a page's head and a remedy each ask ``pages.offered``, which asks the
rule the page gate serves by, so what is offered and what is served agree.
"""

import os
import re
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import RequestFactory, TestCase, modify_settings, override_settings
from django.urls import clear_url_caches, include, path, resolve, reverse, set_script_prefix

from hq.config.urls import urlpatterns as root_urlpatterns
from hq.platform.application import request_context, roles, routes
from hq.platform.application.domains import domain_navigation
from hq.platform.application.pages import Page, PageAction, authorizes_itself, page_refusal
from hq.platform.application.plugins import clear_plugin_composition_cache
from hq.platform.application.routes import routed
from hq.platform.application.security import web_principal
from hq.platform.core.context_processors import nav
from hq.platform.core.test_runner import no_default_role

EXAMPLE = "tests.fixtures.example_hq_plugin"

urlpatterns = [*root_urlpatterns, path("examples/notes/", include(f"{EXAMPLE}.urls"))]

# A value each path converter takes, so every route has an address to offer.
SAMPLES = {
    "int": "1",
    "str": "example",
    "slug": "example",
    "path": "example",
    "uuid": "00000000-0000-4000-8000-000000000000",
}
PARAMETER = re.compile(r"<(?:(?P<converter>\w+):)?\w+>")


def _address(route: str) -> str:
    def sample(match: re.Match[str]) -> str:
        converter = match["converter"] or "str"
        if converter not in SAMPLES:
            raise AssertionError(f"{route} takes a {converter!r}, which this guard has no sample of")
        return SAMPLES[converter]

    return PARAMETER.sub(sample, route)


def _person(username: str, role: str):
    with no_default_role():
        user = get_user_model().objects.create_user(username)
    if role:
        roles.assign(user, role)
    return user


def _opens(user, view) -> bool:
    """What the page gate answers ``user`` at ``view``: let through, or refused."""

    return authorizes_itself(view) or page_refusal(user, view) is None


def _offered_routes(request) -> set[str]:
    return {
        item["url"]
        for entry in nav(request)["nav_entries"]
        for item in (entry["items"] if entry["kind"] == "group" else [entry])
    }


@override_settings(ROOT_URLCONF=__name__)
@modify_settings(INSTALLED_APPS={"append": EXAMPLE})
class OfferedAsServedTests(TestCase):
    """Every link HQ offers by route, the host's and an extension's, follows the gate."""

    def setUp(self):
        installed = os.environ.get("SEVERINO_HQ_PLUGINS", "")
        composed = mock.patch.dict(
            os.environ,
            {
                "SEVERINO_HQ_PLUGINS": ",".join(part for part in (installed, f"{EXAMPLE}.plugin:plugin") if part),
                # A fixture has no signed approval to be admitted by.
                "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
            },
        )
        composed.start()
        self.addCleanup(clear_plugin_composition_cache)
        self.addCleanup(composed.stop)
        clear_plugin_composition_cache()
        self.people = [_person(role or "nobody", role) for role in (*roles.Role.values, "")]

    def test_each_nav_item_is_offered_exactly_when_its_page_opens(self):
        items = domain_navigation()
        self.assertIn("example_plugin:index", {item.route for item in items}, "the example plugin is not composed")
        for user in self.people:
            request = RequestFactory().get("/")
            request.resolver_match = None
            request.user = user
            offered_routes = _offered_routes(request)
            for item in items:
                address = reverse(item.route)
                with self.subTest(person=str(user), route=item.route):
                    self.assertEqual(item.route in offered_routes, _opens(user, resolve(address).func))

    def test_a_link_to_any_routed_page_is_offered_exactly_when_it_opens(self):
        from hq.platform.application.pages import offered

        routes = [route for route in routed() if route.name]
        self.assertIn("example_plugin:index", {route.name for route in routes}, "the example plugin is not routed")
        for route in routes:
            address = _address(route.path)
            view = resolve(address).func
            for user in self.people:
                with self.subTest(person=str(user), route=route.name):
                    self.assertEqual(offered(user, address), _opens(user, view))


class AuditLogLinkTests(TestCase):
    """The audit log is read under its own capability, so only who holds it is led there."""

    def test_a_viewer_is_offered_no_link_to_the_audit_log(self):
        viewer = _person("viewer", roles.Role.VIEWER)
        self.client.force_login(viewer)

        jobs = self.client.get(reverse("jobs:list"))

        self.assertEqual(jobs.status_code, 200)
        self.assertNotIn(reverse("core:audit_list"), [action.url for action in jobs.context["page"].actions])
        self.assertNotIn("core:audit_list", _offered_routes(jobs.wsgi_request))

    def test_an_operator_is_offered_it_in_the_nav_and_on_the_jobs_page(self):
        self.client.force_login(_person("operator", roles.Role.OPERATOR))

        jobs = self.client.get(reverse("jobs:list"))

        self.assertIn(reverse("core:audit_list"), [action.url for action in jobs.context["page"].actions])
        self.assertIn("core:audit_list", _offered_routes(jobs.wsgi_request))


def _first(_request):
    """The page the first URLconf routes ``example`` to."""


def _second(_request):
    """The page the second URLconf routes ``example`` to."""


class FirstURLconf:
    urlpatterns = [path("example/", _first, name="example")]


class SecondURLconf:
    urlpatterns = [path("example/", _second, name="example")]


class UnroutedSectionTests(TestCase):
    """A section whose address HQ routes nothing at is not HQ's to withhold, so the nav draws it."""

    def setUp(self):
        # A resolver of its own, so no lookup an earlier test kept answers this one.
        clear_url_caches()
        routes.forget()
        self.addCleanup(routes.forget)
        self.addCleanup(clear_url_caches)

    def test_the_nav_draws_under_a_script_prefix_the_resolver_does_not_answer(self):
        request = RequestFactory().get("/")
        request.resolver_match = None
        request.user = _person("viewer", roles.Role.VIEWER)
        set_script_prefix("/under/")
        self.addCleanup(set_script_prefix, "/")

        offered_routes = _offered_routes(request)

        self.assertEqual(offered_routes, {item.route for item in domain_navigation()})


class ChangedURLconfTests(TestCase):
    """A route is answered by the URLconf in force, never one a lookup kept from before."""

    def test_a_replaced_urlconf_is_answered_with_its_own_view(self):
        from hq.platform.application.pages import route_view

        with override_settings(ROOT_URLCONF=FirstURLconf):
            self.assertIs(route_view("example"), _first)
        with override_settings(ROOT_URLCONF=SecondURLconf):
            self.assertIs(route_view("example"), _second)


class OnePrincipalPerRequestTests(TestCase):
    """A page asks of every link it draws, and its reader's principal is built once for all of them."""

    def _principals_built(self, user, links: int) -> int:
        request = RequestFactory().get("/")
        request.resolver_match = None
        request.user = user
        audit = reverse("core:audit_list")
        actions = tuple(PageAction(f"Audit {n}", audit) for n in range(links))
        with (
            request_context.bind(request),
            mock.patch("hq.platform.application.pages.web_principal", wraps=web_principal) as built,
        ):
            page = Page("Example", actions=actions, trail=(("Audit", audit),) * links)
            nav(request)
        self.assertEqual((page.actions, page.trail), ((), ()), "a viewer is offered no link to the audit log")
        return built.call_count

    def test_the_principals_built_do_not_grow_with_the_links_a_page_draws(self):
        viewer = _person("viewer", roles.Role.VIEWER)

        self.assertEqual(self._principals_built(viewer, 20), self._principals_built(viewer, 1))
