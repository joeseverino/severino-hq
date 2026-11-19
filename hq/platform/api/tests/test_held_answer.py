"""A call held for a person answers 202 Accepted at the API, apart from the 200
of a call that ran; ``ContractClient`` holds each body to its documented shape."""

import json

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import CapabilityRule

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

PROJECT = {"name": "Held", "slug": "held", "status": "active"}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    # A held answer links its review page at the declared host.
    SEVERINO_SITE_HOST="hq.example.com",
)
class HeldAnswerTests(TestCase):
    client_class = ContractClient

    @classmethod
    def setUpTestData(cls):
        CapabilityRule.objects.create(subject="example-automation", capability="project.create", rule="approve")

    def _post(self, key, project=PROJECT):
        with _serving():
            return self.client.post(
                "/api/v2/capabilities/project.create/",
                data=json.dumps({"payload": project}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                HTTP_IDEMPOTENCY_KEY=key,
            )

    def _rule(self, rule):
        CapabilityRule.objects.filter(subject="example-automation").update(rule=rule)

    def test_a_held_call_resent_under_its_key_answers_202_again(self):
        first = self._post("example-held-again")
        again = self._post("example-held-again")

        self.assertEqual((first.status_code, again.status_code), (202, 202), again.content)
        self.assertTrue(again.json()["data"]["repeated"])
        self.assertEqual(again.json()["data"]["approval"]["id"], first.json()["data"]["approval"]["id"])

    def test_a_change_that_runs_at_once_answers_200_and_a_refused_one_its_own_status(self):
        self._rule(CapabilityRule.Rule.ALLOW)
        ran = self._post("example-held-ran")
        self._rule(CapabilityRule.Rule.DENY)
        refused = self._post("example-held-refused", PROJECT | {"slug": "refused"})

        self.assertEqual(ran.status_code, 200, ran.content)
        self.assertNotIn("status", ran.json()["data"])
        self.assertEqual(refused.status_code, 403, refused.content)
        self.assertFalse(refused.json()["ok"])
