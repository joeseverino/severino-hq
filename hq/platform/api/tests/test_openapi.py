"""The OpenAPI document is served to whom it may be, and holds the API to what it says.

What the API really answers is held by ``ContractClient`` on every request
of the API's tests; the tests below cover the document's own route and the
contract client's refusals.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from hq.platform.application.security import Capability
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import person

from .. import openapi
from ..testing import ContractClient
from .tokens import ISSUER, OTHER_KEY, RESOURCE, _serving, _token

# A list that matched nothing, with every field the collection contract requires.
EMPTY_PAGE = {"items": [], "count": 0, "total": 0, "truncated": False, "next": None, "previous": None}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class ServedDocumentTests(TestCase):
    client_class = ContractClient

    def test_the_served_document_is_the_derived_one(self):
        with _serving():
            response = self.client.get(reverse("hq_api:openapi"), HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), openapi.document())
        self.assertEqual(response["Cache-Control"], "private, no-store")
        # Built once a process for the commands and resources it has.
        self.assertIs(openapi.document(), openapi.document())

    def test_an_anonymous_caller_is_refused(self):
        response = self.client.get(reverse("hq_api:openapi"))
        self.assertEqual(response.status_code, 401)

    def _sign_in(self):
        self.client.force_login(get_user_model().objects.create_user("operator"))

    def test_the_signed_in_operator_reads_it(self):
        self._sign_in()
        response = self.client.get(reverse("hq_api:openapi"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), openapi.document())

    def test_a_signed_in_person_with_no_role_is_refused_it_as_a_page_is(self):
        person(self.client, "example-nobody")

        response = self.client.get(reverse("hq_api:openapi"))

        self.assertEqual(response.status_code, 403)
        denial = AuditLog.objects.filter(action=AuditLog.Action.DENIED).get()
        self.assertEqual(denial.metadata["capability"], Capability.READ)

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_the_operator_reads_it_where_the_token_surface_is_off(self):
        self._sign_in()
        self.assertEqual(self.client.get(reverse("hq_api:openapi")).status_code, 200)
        # Nothing else follows the session.
        self.assertEqual(self.client.get(reverse("hq_api:root")).status_code, 503)

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_an_anonymous_caller_still_gets_the_fail_closed_answer(self):
        self.assertEqual(self.client.get(reverse("hq_api:openapi")).status_code, 503)

    def test_a_session_opens_nothing_else(self):
        self._sign_in()
        self.assertEqual(self.client.get(reverse("hq_api:root")).status_code, 401)

    def test_a_presented_token_is_judged_even_with_a_session(self):
        self._sign_in()
        with _serving():
            response = self.client.get(
                reverse("hq_api:openapi"),
                HTTP_AUTHORIZATION=f"Bearer {_token(key=OTHER_KEY)}",
            )
        self.assertEqual(response.status_code, 401)

    def test_the_root_links_it(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.json()["data"]["links"]["openapi"], reverse("hq_api:openapi"))
