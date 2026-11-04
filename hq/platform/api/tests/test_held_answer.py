"""A call held for a person answers a declared shape on both doors: the API
answers it at 202 Accepted on every capability that changes something, apart
from the 200 of a call that ran, and MCP's ``execute_capability`` beside its
own, and a real held answer meets each."""

import json

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings
from jsonschema import Draft202012Validator

from hq.domains.control_plane.models import CapabilityRule
from hq.platform.application.security import Principal
from hq.platform.mcp.declarations import tool_contract
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp

from .. import openapi
from ..testing import Contract, ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

AGENT = Principal("example-held", "mcp", frozenset({"write_projects", "read"}))
PROJECT = {"name": "Held", "slug": "held", "status": "active"}


def _resolved(document, schema):
    """``schema``, followed through each ``$ref`` to what it names."""

    while "$ref" in schema:
        node = document
        for part in schema["$ref"].removeprefix("#/").split("/"):
            node = node[part]
        schema = node
    return schema


def _offers(document, schema, name):
    """Whether ``schema`` or a branch of its ``anyOf`` is the component ``name``."""

    if schema.get("$ref") == f"{openapi.SCHEMAS}{name}":
        return True
    return any(_offers(document, branch, name) for branch in _resolved(document, schema).get("anyOf", []))


def _data(document, response):
    """The schema of ``data`` in ``response``, an operation's answer at one status."""

    envelope = _resolved(document, response)
    return _resolved(document, envelope["content"]["application/json"]["schema"])["properties"]["data"]


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
    # A held answer links its review page at the declared host.
    SEVERINO_SITE_HOST="hq.example.com",
)
class HeldAnswerTests(TestCase):
    client_class = ContractClient

    @classmethod
    def setUpTestData(cls):
        for subject in ("example-automation", AGENT.actor):
            CapabilityRule.objects.create(
                scope=CapabilityRule.Scope.AGENT, subject=subject, capability="project.create", rule="approve"
            )

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

    def test_every_capability_that_changes_something_answers_the_held_call_at_202_alone(self):
        # A read is never held for a person (``capability_policy.set_rule``).
        document = openapi.document()
        calls = {
            path: operation
            for path, operations in document["paths"].items()
            for operation in operations.values()
            if "x-hq-capability" in operation
        }
        self.assertTrue(calls)
        for path, operation in calls.items():
            with self.subTest(path=path):
                responses = operation["responses"]
                ran = _data(document, responses["200"])
                self.assertFalse(_offers(document, ran, "AwaitingApproval"))
                if operation[openapi.EFFECT_EXTENSION] == "read":
                    self.assertNotIn("202", responses)
                else:
                    self.assertEqual(_data(document, responses["202"]), {"$ref": f"{openapi.SCHEMAS}AwaitingApproval"})

    def test_a_held_call_answers_the_declared_shape_on_both_doors(self):
        response = self._post("example-held-api")
        self.assertEqual(response.status_code, 202, response.content)
        Contract(openapi.document()).validate(["components", "schemas", "AwaitingApproval"], response.json()["data"])

        with set_principal(AGENT):
            answer = async_to_sync(mcp.tools["execute_capability"].call)(
                {"name": "project.create", "payload": PROJECT | {"idempotency_key": "example-held-mcp"}}
            )
        success = tool_contract()["execute_capability"]["outputSchema"]["anyOf"][0]
        held = [
            branch
            for branch in success.get("anyOf", [])
            if branch.get("properties", {}).get("status", {}).get("const") == answer["status"]
        ]
        self.assertEqual(len(held), 1, success)
        Draft202012Validator(held[0]).validate(answer)
