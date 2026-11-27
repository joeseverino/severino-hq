"""The calls agents send that HQ refused, each answered through the SDK's own
client: a call the advertised schema takes runs, and one HQ still refuses says
the call to send instead. The schema judges each first, as a client may."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from jsonschema import Draft202012Validator
from mcp import Client

from hq.platform.application.security import Principal, operator_capabilities

from ..identity import set_principal
from ..server import mcp

READER = Principal("example-reader", "mcp", operator_capabilities())


def _answered(*asked):
    """Each call's verdict by the schema tools/list gave, and the server's answer."""

    async def run():
        async with Client(mcp.server) as client:
            schemas = {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}
            return [
                (Draft202012Validator(schemas[name]).is_valid(arguments), await client.call_tool(name, arguments))
                for name, arguments in asked
            ]

    with set_principal(READER):
        return async_to_sync(run)()


def _message(answer):
    return answer.structured_content["error"]["message"]


class SentCallsTests(TestCase):
    def test_a_call_the_schema_takes_runs(self):
        asked = [
            *(
                ("list_resource", {"name": kind, "limit": 2, "page": 1})
                for kind in ("connections", "credentials", "playbook_scans", "playbooks", "services", "tailnet")
            ),
            ("list_resource", {"name": "audit", "filters": {"on": "2026-11-20"}, "limit": 1}),
        ]
        for (name, arguments), (valid, answer) in zip(asked, _answered(*asked), strict=True):
            with self.subTest(tool=name, arguments=arguments):
                self.assertTrue(valid)
                self.assertFalse(answer.is_error, answer.structured_content)

    def test_an_argument_under_another_name_is_refused_by_the_schema_and_said_as_the_one_it_is(self):
        ((valid, answer),) = _answered(("get_resource", {"kind": "projects", "identifier": "hq"}))

        self.assertFalse(valid)
        self.assertTrue(answer.is_error)
        self.assertIn("send it as name", _message(answer))
        self.assertNotIn("name is required", _message(answer))

    def test_a_value_outside_its_kind_is_refused_naming_the_nearest_it_takes(self):
        (fields, provider) = _answered(
            ("list_resource", {"name": "machines", "fields": ["name", "hostname"]}),
            ("list_resource", {"name": "readings", "filters": {"provider": "cloudflair"}}),
        )

        for (_, answer), nearest in ((fields, "hostnames"), (provider, "cloudflare")):
            with self.subTest(nearest=nearest):
                self.assertTrue(answer.is_error)
                self.assertIn(f"nearest: {nearest}", _message(answer))
                self.assertEqual(answer.structured_content["error"]["details"]["next"]["tool"], "describe_resources")

    def test_several_values_of_a_text_filter_are_said_to_be_sent_comma_separated(self):
        ((_, answer),) = _answered(
            (
                "list_resource",
                {"name": "infrastructure", "filters": {"kinds": ["tailscale.device", "tailscale.policy"]}},
            )
        )

        self.assertTrue(answer.is_error)
        self.assertIn("comma-separated", _message(answer))
