"""No route a request can resolve to puts its name before a person.

Walks every named route the URL resolver holds, as the request middleware
binds it (through the machine API or the web), and reads the words each place
that names an operation builds from it: a history line and an event's page.
"""

import re
from collections.abc import Iterator

from django.test import SimpleTestCase
from django.utils import timezone

from hq.platform.api.urls import app_name as API_APP
from hq.platform.application.history import HistoryLine
from hq.platform.application.labels import human_label
from hq.platform.application.routes import routed
from hq.platform.application.security import API_INTERFACE, WEB_INTERFACE
from hq.platform.core import audit_views
from hq.platform.core.models import AuditLog


def named_routes() -> Iterator[tuple[str, tuple[str, ...], str]]:
    """Each named route: its view name, its namespaces, and the door a request for it comes through."""

    for route in routed():
        if route.name:
            namespaces = tuple(mount.namespace for mount in route.mounts if mount.namespace)
            apps = {mount.app_name for mount in route.mounts}
            yield route.name, namespaces, API_INTERFACE if API_APP in apps else WEB_INTERFACE


class RouteWordsGuard(SimpleTestCase):
    def test_no_operation_is_worded_by_its_route(self):
        routes = list(named_routes())
        self.assertGreater(len(routes), 50)
        for name, namespaces, interface in routes:
            forbidden = {name, human_label(name), *namespaces, *map(human_label, namespaces)}
            rows = tuple(
                AuditLog(
                    object_type="example.record",
                    object_repr=f"Example {letter}",
                    interface=interface,
                    metadata={"operation": name},
                    operation_id="example",
                )
                for letter in "BA"
            )
            # The history's words are read before the page's, which the base lacks a name for.
            self.assertUnworded(HistoryLine(timezone.now(), events=rows).title, forbidden, name, "history")
            self.assertUnworded(audit_views.operation_words(name, interface), forbidden, name, "event page")

    def assertUnworded(self, said: str, forbidden: set[str], route: str, place: str):
        for word in forbidden:
            with self.subTest(route=route, place=place, word=word):
                self.assertIsNone(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", said, re.IGNORECASE), said)
