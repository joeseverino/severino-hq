"""Over the machine API, a missing record is a 404, a stale write or a refused change a 409,
and a value HQ cannot act on a 400."""

import json

from django.test import TestCase, override_settings

from hq.domains.projects.models import Project
from hq.platform.application.tests.test_refusal_text import refusal_of, switched_off

from ..testing import ContractClient
from .test_api import ISSUER, RESOURCE, _serving, _token

SENT = "sent-by-the-caller-7f3a"


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class RecordRefusalTransportTests(TestCase):
    # Every response is also held to the OpenAPI document.
    client_class = ContractClient

    def update(self, target, **envelope):
        with _serving():
            return self.client.post(
                "/api/v2/capabilities/project.update/",
                data=json.dumps({"payload": {"name": "Renamed"}, "target": target, **envelope}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                HTTP_IDEMPOTENCY_KEY=f"key-{target}-{len(envelope)}",
            )

    def test_a_missing_record_is_404_not_found(self):
        response = self.update(SENT)

        self.assertEqual(response.status_code, 404, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "not_found")
        self.assertNotIn(SENT, error["message"])

    def test_a_missing_record_read_is_404_not_found(self):
        for name in ("projects", "machines", "infrastructure.resources"):
            with self.subTest(name), _serving():
                response = self.client.get(
                    f"/api/v2/resources/{name}/{SENT}/",
                    HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
                )
            self.assertEqual(response.status_code, 404, response.content)
            error = response.json()["error"]
            self.assertEqual(error["code"], "not_found")
            self.assertNotIn(SENT, response.content.decode())

    def test_a_stale_write_is_409_conflict(self):
        Project.objects.create(name="HQ", slug=SENT)

        response = self.update(SENT, expected_updated_at="2000-01-01T00:00:00+00:00")

        self.assertEqual(response.status_code, 409, response.content)
        error = response.json()["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertNotIn(SENT, error["message"])
        self.assertEqual(Project.objects.get(slug=SENT).name, "HQ")

    def test_a_current_write_is_200(self):
        project = Project.objects.create(name="HQ", slug="hq")

        response = self.update("hq", expected_updated_at=project.updated_at.isoformat())

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(Project.objects.get(pk=project.pk).name, "Renamed")

    def test_a_change_the_record_refuses_is_409_precondition_with_its_reason(self):
        resource = switched_off()
        reason = refusal_of(resource)

        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/infrastructure.reconcile/",
                data=json.dumps({"payload": {}, "target": resource.key}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='manage_infrastructure')}",
                HTTP_IDEMPOTENCY_KEY="key-refused",
            )

        self.assertEqual(response.status_code, 409, response.content)
        self.assertEqual(response.json()["error"], {"code": "precondition", "message": reason})

    def test_a_value_hq_cannot_act_on_is_400_invalid_input_in_its_own_words(self):
        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/lookup.address/",
                data=json.dumps({"payload": {"address": SENT}}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='look_up_public_records')}",
            )

        self.assertEqual(response.status_code, 400, response.content)
        self.assertEqual(response.json()["error"], {"code": "invalid_input", "message": "That is not an IP address."})
