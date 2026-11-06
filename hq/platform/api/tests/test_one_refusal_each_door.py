"""The same malformed narrowing or call gets the same refusal on every door: the
machine API's query string or body, an MCP tool's arguments and the operator's
``hq_call`` answer with one code, one sentence and one set of details."""

import json
from urllib.parse import urlencode

from asgiref.sync import async_to_sync
from django.test import TestCase, override_settings

from hq.platform.application.capabilities import adapter_refusal
from hq.platform.application.security import Capability, Principal, agent_principal
from hq.platform.application.tests.host_only import host_only
from hq.platform.core.management.commands.hq_call import answered
from hq.platform.mcp.identity import set_principal
from hq.platform.mcp.server import mcp
from hq_sdk.capabilities import execute_capability as sdk_execute

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ}))
# A caller that may run every change ``CALLS`` names, so what each door refuses is what the call carried.
WRITER = Principal("example-agent", "mcp", frozenset({Capability.READ, "write_projects", "write_expenses"}))
FOCUS = "resource:example-focus"
# Each read's path, its MCP tool, the integer it narrows by, and values as an MCP
# caller sends them; the API's query string carries each one's JSON text.
# Calls whose target or stale-write token is not of its kind, each to a capability that takes a target.
CALLS = (
    ("expense.update", {"target": "example-not-digits"}),
    ("project.update", {"target": ""}),
    ("project.update", {"target": "example", "expected_updated_at": "example-not-a-time"}),
    ("project.update", {"target": "example", "expected_updated_at": 1}),
    ("expense.update", {"target": True}),
)
READS = (
    ("topology", "get_topology", "depth", ("abc", "2 ", "5_0", "", True, 1.0, 9, -1), {"focus": FOCUS}),
    ("findings", "get_findings", "limit", ("many", "+3", "1.0", "", False, 0, -3), {}),
)
# A text narrowing that names none of its choices, on each read.
CHOICES = (
    ("topology", "get_topology", {"lens": "example-unknown"}),
    ("topology", "get_topology", {"focus": FOCUS, "direction": "sideways"}),
    ("findings", "get_findings", {"rule": "example-unknown"}),
    ("findings", "get_findings", {"severity": "critical"}),
)


def _query_text(value):
    return value if isinstance(value, str) else json.dumps(value)


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class OneRefusalEachDoorTests(TestCase):
    client_class = ContractClient

    def _api(self, query, path="topology"):
        with _serving(), host_only():
            return self.client.get(f"/api/v2/{path}/?{query}", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")

    def _mcp(self, name, arguments, principal=AGENT):
        tool = mcp.tools[name]
        with set_principal(principal):
            try:
                return async_to_sync(tool.call)(arguments)
            except Exception as exc:  # noqa: BLE001 - mapped as server.py maps a tool's failure
                return adapter_refusal(tool.name, exc, principal=principal)

    def test_an_integer_that_is_not_a_count_is_refused_alike_on_every_door(self):
        for path, tool, field, values, rest in READS:
            for value in values:
                arguments = {**rest, field: value}
                with self.subTest(path=path, value=value):
                    api = self._api(urlencode({**rest, field: _query_text(value)}), path)
                    with host_only():
                        over_mcp = self._mcp(tool, arguments)
                        operator = answered(json.dumps({"tool": tool, "arguments": arguments}))

                    self.assertEqual(api.status_code, 400, api.content)
                    refusal = api.json()["error"]
                    self.assertEqual(refusal["code"], "invalid_input")
                    for door, answer in (("mcp", over_mcp), ("hq_call", operator)):
                        with self.subTest(door=door):
                            self.assertFalse(answer["ok"])
                            self.assertEqual(answer["error"], refusal)

    def test_a_choice_none_declares_is_refused_alike_on_every_door(self):
        for path, tool, arguments in CHOICES:
            with self.subTest(path=path, arguments=arguments):
                api = self._api(urlencode(arguments), path)
                with host_only():
                    over_mcp = self._mcp(tool, arguments)

                self.assertEqual(api.status_code, 400, api.content)
                self.assertFalse(over_mcp["ok"])
                self.assertEqual(over_mcp["error"], api.json()["error"])

    def test_a_count_in_digits_reads_as_one(self):
        response = self._api("depth=1")

        self.assertEqual(response.status_code, 200, response.content)

    def test_a_narrowing_sent_twice_is_refused(self):
        for query in ("depth=1&depth=2", "lens=unobserved-resources&lens=unobserved-resources"):
            with self.subTest(query=query):
                response = self._api(query)

                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_a_call_carrying_a_target_or_stamp_not_of_its_kind_is_refused_alike_on_every_door(self):
        """Asked of a caller that may run the change, so each door answers for what the call carried."""

        for name, carried in CALLS:
            arguments = {"name": name, "payload": {}, **carried}
            with self.subTest(name=name, carried=carried):
                with _serving():
                    api = self.client.post(
                        f"/api/v2/capabilities/{name}/",
                        data=json.dumps({"payload": {}, **carried}),
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects write_expenses')}",
                        HTTP_IDEMPOTENCY_KEY="example-envelope",
                    )
                over_mcp = self._mcp("execute_capability", arguments, WRITER)
                operator = answered(json.dumps({"tool": "execute_capability", "arguments": arguments}))
                # The door an extension runs a capability through, as itself.
                with host_only():
                    extension = sdk_execute(name, {}, principal=WRITER, **carried)

                self.assertEqual(api.status_code, 400, api.content)
                refusal = api.json()["error"]
                self.assertEqual(refusal["code"], "invalid_input")
                for door, answer in (("mcp", over_mcp), ("hq_call", operator), ("hq_sdk", extension)):
                    with self.subTest(door=door):
                        self.assertFalse(answer["ok"])
                        self.assertEqual(answer["error"], refusal)

    def test_a_caller_without_the_grant_is_refused_alike_whatever_it_carried(self):
        """No door tells a reader the shape of target a change it may not run would have taken."""

        # The reader a token granting only `read` becomes, on every door.
        reader = agent_principal(AGENT.actor, "mcp", frozenset({"read"}))
        for name, carried in CALLS:
            arguments = {"name": name, "payload": {}, **carried}
            with self.subTest(name=name, carried=carried):
                with _serving():
                    api = self.client.post(
                        f"/api/v2/capabilities/{name}/",
                        data=json.dumps({"payload": {}, **carried}),
                        content_type="application/json",
                        HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
                        HTTP_IDEMPOTENCY_KEY="example-envelope",
                    )
                over_mcp = self._mcp("execute_capability", arguments, reader)
                with host_only():
                    extension = sdk_execute(name, {}, principal=reader, **carried)

                self.assertEqual(api.status_code, 403, api.content)
                self.assertEqual(api.json()["error"]["code"], "insufficient_scope")
                for door, answer in (("mcp", over_mcp), ("hq_sdk", extension)):
                    with self.subTest(door=door):
                        self.assertFalse(answer["ok"])
                        self.assertEqual(answer["error"]["code"], "insufficient_scope")
