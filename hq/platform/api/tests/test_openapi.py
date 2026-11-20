"""The OpenAPI document is served to whom it may be, and holds the API to what it says.

What the API really answers is held by ``ContractClient`` on every request
of the API's tests; the tests below cover the document's own route and the
contract client's refusals.
"""

import json

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from jsonschema import ValidationError

from hq.platform.application.security import Capability
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import person

from .. import openapi
from ..testing import ContractClient
from .tokens import ISSUER, OTHER_KEY, RESOURCE, _serving, _token

# A list that matched nothing, with every field the collection contract requires.
EMPTY_PAGE = {"items": [], "count": 0, "total": 0, "truncated": False, "next": None}


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


class ContractClientTests(SimpleTestCase):
    """The contract client refuses what the document does not describe."""

    def setUp(self):
        self.client = ContractClient()

    def _answer(self, url, status, body, method="GET"):
        response = HttpResponse(json.dumps(body), status=status, content_type="application/json")
        self.client._conform(method.lower(), url, "", response)

    def test_an_undocumented_status_fails(self):
        with self.assertRaisesRegex(AssertionError, "does not list"):
            self._answer("/api/v2/", 418, {"ok": False, "error": {"code": "x", "message": "y"}})

    def test_an_undocumented_path_fails(self):
        with self.assertRaisesRegex(AssertionError, "not in the OpenAPI document"):
            self._answer("/api/v2/nowhere/", 200, {})

    def test_a_body_off_its_schema_fails(self):
        with self.assertRaises(ValidationError):
            self._answer("/api/v2/", 200, {"ok": True, "data": {"service": "severino-hq"}})

    def test_a_whole_answer_missing_a_field_its_record_promises_fails(self):
        saved = {"created": True, "message": "Saved.", "asset": {}}
        with self.assertRaisesRegex(ValidationError, "'slug' is a required property"):
            self._answer("/api/v2/capabilities/asset.create/", 200, {"ok": True, "data": saved}, method="POST")

    def test_resource_collections_accept_projection_metadata(self):
        self._answer(
            "/api/v2/resources/projects/",
            200,
            {"ok": True, "data": {**EMPTY_PAGE, "filters": {"query": "example"}}},
        )

    def test_resource_collection_metadata_does_not_relax_items_or_count(self):
        for data in (
            {"items": "wrong", "count": 0},
            {"items": [], "count": "wrong"},
            {"items": []},
            {"count": 0},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                self._answer("/api/v2/resources/projects/", 200, {"ok": True, "data": data})

    def test_an_error_off_the_envelope_fails(self):
        with self.assertRaises(ValidationError):
            self._answer("/api/v2/", 401, {"ok": False, "detail": "no"})

    def test_a_concrete_path_is_matched_before_its_template(self):
        contract = self.client.contract
        self.assertEqual(contract.path("/api/v2/capabilities/project.create/"), "/api/v2/capabilities/project.create/")
        self.assertEqual(contract.path("/api/v2/capabilities/nope/"), "/api/v2/capabilities/{name}/")
