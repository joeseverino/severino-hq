"""A call held for a person answers a declared shape on both doors: the API
document offers it beside the success on every capability's 200, and MCP's
``execute_capability`` beside its own, and a real held answer meets each."""

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


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_AGENT_ENABLE_WRITES=True,
)
class HeldAnswerTests(TestCase):
    client_class = ContractClient

    @classmethod
    def setUpTestData(cls):
        for subject in ("example-automation", AGENT.actor):
            CapabilityRule.objects.create(
                scope=CapabilityRule.Scope.AGENT, subject=subject, capability="project.create", rule="approve"
            )

    def test_every_capability_offers_the_held_answer_beside_its_own(self):
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
                envelope = _resolved(document, operation["responses"]["200"])
                schema = _resolved(document, envelope["content"]["application/json"]["schema"])
                self.assertTrue(_offers(document, schema["properties"]["data"], "AwaitingApproval"))

    def test_a_held_call_answers_the_declared_shape_on_both_doors(self):
        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/project.create/",
                data=json.dumps({"payload": PROJECT}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                HTTP_IDEMPOTENCY_KEY="example-held-api",
            )
        self.assertEqual(response.status_code, 200, response.content)
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
