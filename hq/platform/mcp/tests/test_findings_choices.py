"""get_findings names the rules and severities a model may narrow by, from the
rule registry, its listed outputSchema names every field it answers, and every
action a finding carries is a tool call an agent can make, never a web path."""

from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone
from mcp import Client

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.action_links import ActionLink
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.finding_model import Finding, FindingRemedy
from hq.platform.application.findings import RULES, SEVERITIES, derive_findings
from hq.platform.application.security import Capability, Principal
from hq.platform.application.tests.inventory_records import kind_read
from hq.platform.application.topology_model import Topology, TopologyEdge, TopologyNode
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..identity import set_principal
from ..server import mcp

REWRITE = "adguard.rewrite"
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


def actions(finding):
    """Every action ``finding`` carries, by where it sits."""

    yield from (("remedies", call) for call in finding["remedies"])
    yield from (("offers", call) for call in finding["offers"])
    yield from (("investigations", call) for call in finding["investigations"])
    for step in (finding["workflow"] or {}).get("steps", ()):
        yield from ((f"workflow.{step['phase']}", call) for call in step["calls"])


def paths(value, where="answer"):
    """Where ``value`` holds a web path: a key a link is followed by, or a value that is an HQ path or a URL."""

    if isinstance(value, dict):
        for key, inner in value.items():
            if key in ("url", "method", "href"):
                yield f"{where}.{key}"
            yield from paths(inner, f"{where}.{key}")
    elif isinstance(value, list):
        for index, inner in enumerate(value):
            yield from paths(inner, f"{where}[{index}]")
    elif isinstance(value, str) and value.startswith(("/", "http://", "https://")):
        yield where


class FindingActionsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        # The estate the showcase serves, and a record its sweep skipped, whose remedy is a command.
        with throwaway_media(), seeds.built("showcase"):
            pass
        now = timezone.now()
        for key, when in (("example-skipped", now - timedelta(hours=9)), ("example-swept", now)):
            spec = {"domain": f"{key}.example.test", "answer": "192.0.2.10"}
            ManagedResource.objects.create(
                key=key,
                kind=REWRITE,
                spec=spec,
                last_observed_at=when,
                generation=1,
                observed_generation=1,
                status=spec,
                conditions=[{"type": "Ready", "status": True, "reason": "Observed", "message": ""}],
            )
            kind_read(REWRITE, when)

    def test_every_action_on_a_seeded_finding_is_a_tool_call_and_no_answer_holds_a_path(self):
        listed, (answer,) = session(("get_findings", {}))

        self.assertFalse(answer.is_error)
        found = answer.structured_content["findings"]
        carried = [(where, call) for finding in found for where, call in actions(finding)]
        # Each place an action sits was read: a remedy, an offer, an investigation and a workflow's check.
        self.assertLessEqual(
            {"remedies", "offers", "investigations", "workflow.verify"},
            {where for where, _ in carried},
            [(finding["rule"], finding["subject"]) for finding in found],
        )
        for where, call in carried:
            with self.subTest(where=where, call=call):
                self.assertIn(call["tool"], listed)
                self.assertIsInstance(call["arguments"], dict)
        self.assertEqual(list(paths(answer.structured_content)), [])

    def test_each_read_a_finding_offers_answers_and_each_command_is_addressed_as_it_takes(self):
        _, (answer,) = session(("get_findings", {}))
        carried = [call for finding in answer.structured_content["findings"] for _, call in actions(finding)]
        reads = [call for call in carried if call["tool"] != "execute_capability"]
        commands = [call["arguments"] for call in carried if call["tool"] == "execute_capability"]

        # Followed as an agent would, each read answers rather than refuses.
        _, answered = session(*((call["tool"], call["arguments"]) for call in reads))
        for call, result in zip(reads, answered, strict=True):
            with self.subTest(call=call):
                self.assertFalse(result.is_error, result.structured_content)
        registry = capability_registry()
        for arguments in commands:
            with self.subTest(command=arguments):
                spec = registry[arguments["name"]]
                # A target exactly where the command takes one, or execute_capability refuses it.
                self.assertEqual(arguments["target"] is not None, bool(spec.target_kind))

    def test_the_listed_shape_closes_every_action_over_its_tool_and_arguments(self):
        listed, _ = session()
        item = listed["get_findings"].output_schema["anyOf"][0]["properties"]["findings"]["items"]

        def shapes(schema):
            yield from schema.get("anyOf") or (schema,)

        lists = {name: item["properties"][name]["items"] for name in ("remedies", "offers", "investigations")}
        workflow = item["properties"]["workflow"]
        lists["workflow"] = workflow["properties"]["steps"]["items"]["properties"]["calls"]["items"]
        for name, schema in lists.items():
            for shape in shapes(schema):
                with self.subTest(actions=name, tool=shape["properties"]["tool"]):
                    self.assertFalse(shape["additionalProperties"])
                    self.assertIn(shape["properties"]["tool"]["const"], listed)
                    self.assertFalse(shape["properties"]["arguments"]["additionalProperties"])


STALE = "2000-01-01T00:00:00+00:00"
# A controller whose sweep went stale over two kinds: its remedy is a command
# on its node, so its plan acts and then checks, reading every connection again.
CONTROLLER_STALE = Topology(
    nodes=(
        TopologyNode(
            "controller:one",
            "controller",
            "Example",
            "Controller",
            actions=(
                ActionLink(
                    "command",
                    "Refresh",
                    "infrastructure_change",
                    "/commands/infrastructure.controller.refresh/",
                    capability="infrastructure.controller.refresh",
                ),
            ),
        ),
        TopologyNode("connection:one", "connection", "Provider", "Connection"),
        TopologyNode("ability:a", "ability", "Read A", "Ability"),
        TopologyNode("ability:b", "ability", "Read B", "Ability"),
        TopologyNode("resource:a", "resource", "A", "Resource", kind_key="example.a", observed_at=STALE),
        TopologyNode("resource:b", "resource", "B", "Resource", kind_key="example.b", observed_at=STALE),
    ),
    edges=(
        TopologyEdge("carries", "controller:one", "connection:one", "carries", "Carries"),
        TopologyEdge("enables-a", "connection:one", "ability:a", "enables", "Enables"),
        TopologyEdge("enables-b", "connection:one", "ability:b", "enables", "Enables"),
        TopologyEdge("governs-a", "ability:a", "resource:a", "governs", "Governs"),
        TopologyEdge("governs-b", "ability:b", "resource:b", "governs", "Governs"),
    ),
)


class FindingPlanTests(TestCase):
    def test_a_plan_acts_through_its_command_and_checks_by_reading_again_then_asking_again(self):
        raised = derive_findings(CONTROLLER_STALE, principal=READER)
        with mock.patch("hq.platform.application.findings.estate_findings", return_value=raised):
            _, (answer,) = session(("get_findings", {}))

        (finding,) = answer.structured_content["findings"]
        act, verify = finding["workflow"]["steps"]
        refresh = "infrastructure.controller.refresh"
        self.assertEqual(
            [(call["tool"], call["arguments"]) for call in act["calls"]],
            [("execute_capability", {"name": refresh, "target": None, "payload": {}})],
        )
        # The read the check asks for is its payload, then the check itself is this read again.
        self.assertEqual(
            [(call["tool"], call["arguments"]) for call in verify["calls"]],
            [
                ("execute_capability", {"name": refresh, "target": None, "payload": {"every_connection": True}}),
                ("get_findings", {"rule": finding["rule"]}),
            ],
        )
        self.assertEqual(list(paths(answer.structured_content)), [])
