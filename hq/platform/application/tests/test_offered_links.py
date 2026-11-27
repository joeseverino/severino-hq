"""HQ offers a link to one of its pages only to a person who may open it.

The nav, a page's head and a remedy each ask ``pages.offered``, which asks the
rule the page gate serves by, so what is offered and what is served agree.
"""

import os
from unittest import mock

from django.test import TestCase, modify_settings, override_settings
from django.urls import include, path, resolve

from hq.config.urls import urlpatterns as root_urlpatterns
from hq.platform.application import roles
from hq.platform.application.pages import authorizes_itself, page_refusal
from hq.platform.application.routes import routed
from hq.platform.core.testing import person

from .addresses import address

EXAMPLE = "tests.fixtures.example_hq_plugin"

urlpatterns = [*root_urlpatterns, path("examples/notes/", include(f"{EXAMPLE}.urls"))]


def _person(username: str, role: str):
    user = person(username=username)
    if role:
        roles.assign(user, role)
    return user


def _opens(user, view) -> bool:
    """What the page gate answers ``user`` at ``view``: let through, or refused."""

    return authorizes_itself(view) or page_refusal(user, view) is None


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
        self.addCleanup(composed.stop)
        self.people = [_person(role or "nobody", role) for role in (*roles.Role.values, "")]

    def test_a_link_to_any_routed_page_is_offered_exactly_when_it_opens(self):
        from hq.platform.application.pages import offered

        routes = [route for route in routed() if route.name]
        self.assertIn("example_plugin:index", {route.name for route in routes}, "the example plugin is not routed")
        for route in routes:
            url = address(route.path)
            view = resolve(url).func
            for user in self.people:
                with self.subTest(person=str(user), route=route.name):
                    self.assertEqual(offered(user, url), _opens(user, view))
