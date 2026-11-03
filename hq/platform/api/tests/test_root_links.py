"""The API root links each of the API's own reads by its route alone: a
parameterless named route added to the URLconf is linked, and typed in the
document, with no other edit."""

from contextlib import contextmanager
from typing import Any
from unittest import mock

from django.contrib.auth.decorators import login_not_required
from django.test import TestCase, override_settings
from django.urls import URLResolver, clear_url_caches, get_resolver, path

from .. import openapi, urls, views
from .tokens import ISSUER, RESOURCE, _serving, _token


@login_not_required
@views._endpoint(("GET",), title="Example read", data=dict[str, Any])
def example_read(_request):
    """What an example read answers."""

    return views._ok({})


# A read the URLconf does not have, mounted as ``public`` mounts every API route.
EXAMPLE = path("v2/example-read/", example_read, name="example_read")


@contextmanager
def _routed(*added):
    """The API's URLconf with ``added`` routes, as a new line in ``urls.py`` would leave it."""

    mount = next(
        entry
        for entry in get_resolver().url_patterns
        if isinstance(entry, URLResolver) and entry.app_name == urls.app_name
    )
    patterns = [*urls.urlpatterns, *added]
    clear_url_caches()
    try:
        with (
            mock.patch.object(urls, "urlpatterns", patterns),
            mock.patch.dict(mount.__dict__, {"url_patterns": patterns}),
        ):
            yield
    finally:
        clear_url_caches()


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class RootLinksTests(TestCase):
    def _links(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["data"]["links"]

    def test_a_new_read_is_linked_and_typed_by_its_route(self):
        with _routed(EXAMPLE):
            links = self._links()
            typed = openapi.document()["components"]["schemas"]["RootLinks"]

        self.assertEqual(links["example_read"], "/api/v2/example-read/")
        # A route that takes a parameter is no read to link, nor is the root itself.
        self.assertFalse({"root", "execute", "resource-list", "resource-detail"} & set(links))
        self.assertIn("example_read", typed["required"])
        # Said in the summary of the view it routes to, as its operation opens.
        self.assertEqual(typed["properties"]["example_read"]["description"], "What an example read answers.")
