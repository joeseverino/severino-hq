"""A retry key is one key, whichever way it arrives: the API's header and the payload's field name the same one.
That a key replays across doors is ``test_promises_each_door.py``'s."""

import json

from django.test import TestCase, override_settings

from hq.domains.projects.models import Project

from .tokens import ISSUER, RESOURCE, _serving, _token

CLIENT = "example-agent"
PROJECT = {"name": "Once", "slug": "once", "status": "active"}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
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
