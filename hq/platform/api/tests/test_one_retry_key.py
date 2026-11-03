"""A retry key is one key, whichever way it arrives: the API's header and the payload's field name the same one."""

import json

from django.test import TestCase, override_settings

from hq.domains.projects.models import Project
from hq.platform.application import tokens
from hq.platform.mcp import services
from hq.platform.mcp.identity import set_principal

from .tokens import ISSUER, RESOURCE, _serving, _token

CLIENT = "example-agent"
PROJECT = {"name": "Once", "slug": "once", "status": "active"}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class OneKeyTests(TestCase):
    def post(self, payload, key=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(CLIENT, scope='write_projects')}"}
        if key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = key
        with _serving():
            return self.client.post(
                "/api/v2/capabilities/project.create/",
                data=json.dumps({"payload": payload}),
                content_type="application/json",
                **headers,
            )

    def test_the_payloads_key_alone_is_enough_over_the_api(self):
        first = self.post(PROJECT | {"idempotency_key": "example-once"})
        again = self.post(PROJECT | {"idempotency_key": "example-once"})

        self.assertEqual(first.status_code, 200, first.json())
        self.assertEqual(again.json(), first.json())
        self.assertEqual(again["Idempotency-Replayed"], "true")
        self.assertEqual(Project.objects.filter(slug="once").count(), 1)

    def test_two_different_keys_in_one_request_are_refused(self):
        response = self.post(PROJECT | {"idempotency_key": "example-one"}, key="example-two")

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_idempotency_key")
        self.assertFalse(Project.objects.exists())

    def test_a_key_used_over_mcp_replays_over_the_api(self):
        claims = {"client_id": CLIENT, "sub": CLIENT, "scope": "write_projects"}
        with set_principal(tokens.token_principal(claims, "mcp")):
            first = services.execute_capability("project.create", PROJECT | {"idempotency_key": "example-once"})

        again = self.post(PROJECT, key="example-once")

        self.assertTrue(first["ok"], first)
        self.assertEqual(again.status_code, 200, again.json())
        self.assertEqual(again["Idempotency-Replayed"], "true")
        self.assertEqual(Project.objects.filter(slug="once").count(), 1)
