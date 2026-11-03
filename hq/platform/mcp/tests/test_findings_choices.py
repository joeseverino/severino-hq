"""get_findings names the rules and severities a model may narrow by, from the
rule registry, and its listed outputSchema names every field it answers."""

from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase
from mcp import Client

from hq.platform.application.finding_model import Finding, FindingRemedy
from hq.platform.application.findings import RULES, SEVERITIES
from hq.platform.application.security import Capability, Principal

from ..identity import set_principal
from ..server import mcp

READER = Principal("example-reader", "mcp", frozenset(Capability))
RAISED = Finding(
    rule=RULES[0].name,
    subject="example-subject",
    title="Example",
    severity="attention",
    explanation="Example.",
    evidence=(("Seen", "example"),),
    remedies=(FindingRemedy("infrastructure.resource.reconcile", "example-subject"),),
)


def session(*asked):
    """The tool list and each call's result, every success checked against its outputSchema by the client."""

    async def run():
        async with Client(mcp.server) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            answered = [await client.call_tool(name, arguments) for name, arguments in asked]
            return listed, answered

    with set_principal(READER):
        return async_to_sync(run)()


class FindingsChoicesTests(TestCase):
    def test_the_rule_and_severity_choices_are_the_registrys(self):
        listed, _ = session()
        properties = listed["get_findings"].input_schema["properties"]

        def values(name):
            return next(option["enum"] for option in properties[name]["anyOf"] if "enum" in option)

        self.assertEqual(values("rule"), [rule.name for rule in RULES])
        self.assertEqual(values("severity"), list(SEVERITIES))

    def test_a_rule_the_registry_lacks_is_refused(self):
        _, (answer,) = session(("get_findings", {"rule": "example-unknown"}))

        self.assertTrue(answer.is_error)
        self.assertEqual(answer.structured_content["error"]["code"], "invalid_input")

    def test_the_answer_carries_no_field_its_schema_does_not_name(self):
        with mock.patch("hq.platform.application.findings.estate_findings", return_value=(RAISED,)):
            listed, (answer,) = session(("get_findings", {"rule": RAISED.rule}))

        self.assertFalse(answer.is_error)
        (finding,) = answer.structured_content["findings"]
        # Every object the schema names closes over its fields, so the client's check
        # above refused any field it does not name; each named here was read.
        success = listed["get_findings"].output_schema["anyOf"][0]
        item = success["properties"]["findings"]["items"]
        self.assertEqual(set(answer.structured_content), set(success["properties"]))
        self.assertEqual(set(finding), set(item["properties"]))
        self.assertEqual(set(finding["remedies"][0]), set(item["properties"]["remedies"]["items"]["properties"]))
        self.assertFalse(item["additionalProperties"])
