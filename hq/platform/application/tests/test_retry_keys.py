"""One retry convention for every capability that changes or queues something.

Each takes an optional ``idempotency_key``, declared once from its effect; a
repeat carrying the same key returns the first result. A ``read`` takes none.
"""

from django.test import TestCase

from hq.domains.control_plane.models import CapabilityRule
from hq.domains.projects.models import Project

from ..capabilities import describe_capabilities, execute_capability
from ..integration_specs import IDEMPOTENCY_FIELD
from ..security import Capability, Principal, cli_principal


class DeclaredOnceTests(TestCase):
    def test_every_capability_that_is_not_a_read_publishes_the_key_and_none_requires_it(self):
        """Read from the catalog a caller is handed, so it holds what an agent sends."""

        for item in describe_capabilities(principal=cli_principal())["capabilities"]:
            schema = item["input_schema"]
            with self.subTest(capability=item["name"]):
                if item["effect"] == "read":
                    self.assertNotIn(IDEMPOTENCY_FIELD, schema.get("properties", {}))
                    continue
                self.assertIn(IDEMPOTENCY_FIELD, schema["properties"])
                self.assertNotIn(IDEMPOTENCY_FIELD, schema.get("required", ()))


class CommandKeyTests(TestCase):
    """The runner keeps the retry key: no command is handed it to answer a retry a second way."""

    def test_a_command_that_declares_the_key_is_refused(self):
        from dataclasses import dataclass
        from typing import TypedDict

        from ..integration_specs import CapabilitySpec

        @dataclass(frozen=True)
        class Keyed:
            idempotency_key: str = ""

        class KeyedDict(TypedDict, total=False):
            idempotency_key: str

        for command in (Keyed, KeyedDict):
            with self.subTest(command=command.__name__), self.assertRaisesMessage(TypeError, IDEMPOTENCY_FIELD):
                CapabilitySpec(
                    name="example.keyed.set",
                    summary="Example.",
                    effect="remote_write",
                    required_capability=Capability.READ,
                    command_type=command,
                    handler=lambda command, *, principal: {"ok": True},
                )


class PolicyTests(TestCase):
    """A call refused or held keeps nothing under its key."""

    AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, "write_projects"}))

    def rule(self, rule):
        CapabilityRule.objects.update_or_create(
            subject="example-agent",
            capability="project.create",
            defaults={"rule": rule},
        )

    def test_the_same_key_runs_once_a_refusing_or_holding_rule_is_lifted(self):
        for rule, answered in (
            (CapabilityRule.Rule.DENY, "denied_by_policy"),
            (CapabilityRule.Rule.APPROVE, "awaiting_approval"),
        ):
            payload = {"name": "Example", "slug": rule, IDEMPOTENCY_FIELD: f"create-{rule}"}
            with self.subTest(rule=rule):
                self.rule(rule)
                answer = execute_capability("project.create", payload, principal=self.AGENT)
                self.assertEqual(answer.get("status") or answer["error"]["code"], answered)
                self.assertFalse(Project.objects.filter(slug=rule).exists())
                self.rule(CapabilityRule.Rule.ALLOW)

                self.assertNotIn("status", execute_capability("project.create", payload, principal=self.AGENT))
                self.assertTrue(Project.objects.filter(slug=rule).exists())

    def test_a_handlers_own_refusal_keeps_nothing_under_its_key(self):
        from hq.platform.application.projects import DEPENDENCY_UPDATES

        project = Project.objects.create(name="Example", slug="example")
        payload = {"dependency_updates": False, IDEMPOTENCY_FIELD: "switch-once"}

        def switch():
            return execute_capability(DEPENDENCY_UPDATES, payload, principal=cli_principal(), target="example")

        self.assertEqual(switch()["error"]["code"], "precondition")
        project.repository_url = "https://github.com/example/example"
        project.save()

        self.assertTrue(switch()["ok"])
