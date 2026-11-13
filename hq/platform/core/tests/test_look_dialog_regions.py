"""dev:look answers a dialog's region through the server on any page it opens the dialog on."""

import os
from unittest import mock

from django.test import SimpleTestCase, tag

from . import look

ASKED = {"usage_pages": "example"}


class _Route:
    """A browser's request as the look's route handler takes it, and what it was answered."""

    def __init__(self, path, headers):
        url = f"https://example.com{path}"
        self.request = mock.Mock(url=url, method="GET", headers=headers, is_navigation_request=lambda: False)
        self.fulfilled = None

    def fulfill(self, **answer):
        self.fulfilled = answer


@tag("source")
class DialogRegionTests(SimpleTestCase):
    """A dialog fills itself from its region's own request, which the look
    answers through the server whatever page it was opened on."""

    def test_a_dialog_asked_on_pages_answered_from_memory_is_served(self):
        for kept in ("estate_client", "addresses"):
            self.addCleanup(lambda kept=kept: kept in vars(look.Look) and delattr(look.Look, kept))
        environment = {**ASKED, "usage_dialog": "connection"}
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch.object(look, "stress_pages", return_value={"connections": "<main></main>"}),
            mock.patch.object(look.Look, "serve_estate", return_value="served") as serve,
            mock.patch.object(look.Look, "asked", ["connections"], create=True),
        ):
            look.Look.render()
        serve.assert_called_once()
        self.assertEqual(look.Look.estate_client, "served")

    def answered_by(self, path, headers, on="/connections/"):
        """Who answers ``path`` asked with ``headers`` from the gate page at ``on``: "memory" or "server"."""

        looker = look.Look.__new__(look.Look)
        looker.pages = {"connections": "<main></main>", "connection": "<main></main>"}
        looker.on_estate, looker.answered = False, {}
        looker.page = mock.Mock(url=f"https://example.com{on}")
        response = mock.Mock(status_code=200, get=lambda _name, default="": default, getvalue=lambda: b"")
        looker.estate_client = mock.Mock(get=mock.Mock(return_value=response))
        route = _Route(path, headers)
        with mock.patch.object(look.LayoutBrowserTests, "respond") as from_memory:
            looker.respond(route)
        return "memory" if from_memory.called else "server"

    def test_a_region_of_another_page_is_asked_of_the_server(self):
        region = {"x-fragment": "connection", "x-requested-with": "XMLHttpRequest"}
        self.assertEqual(self.answered_by("/connection/", region), "server")
        # The page itself, and its own region, are the page drawn in memory.
        self.assertEqual(self.answered_by("/connection/", {}), "memory")
        self.assertEqual(self.answered_by("/connections/", region), "memory")
