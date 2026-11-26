"""One retry convention for every capability that changes or queues something.

Each takes an optional ``idempotency_key``, declared once from its effect; a
repeat carrying the same key returns the first result. A ``read`` takes none.
"""

from django.test import TestCase

from hq.domains.control_plane.models import CapabilityRule, OperationRequest, ReadRequest
from hq.domains.projects.models import Project
from hq.platform.core.models import IdempotencyRecord
from hq.platform.core.testing import declare, managing_everything

from ..capabilities import describe_capabilities, execute_capability
from ..integration_specs import IDEMPOTENCY_FIELD
from ..security import Capability, Principal, cli_principal

REFRESH = "infrastructure.controller.refresh"
RECONCILE = "infrastructure.resource.reconcile"


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


class RefreshTests(TestCase):
    def ask(self, principal=None, **payload):
        return execute_capability(REFRESH, payload, principal=principal or cli_principal())

    def test_it_still_runs_without_one_and_records_nothing_to_replay(self):
        self.assertTrue(self.ask(every_connection=True)["ok"])

        self.assertFalse(IdempotencyRecord.objects.exists())

    def test_a_repeat_with_the_same_key_returns_the_first_result_and_asks_once(self):
        first = self.ask(every_connection=True, idempotency_key="refresh-once")
        ReadRequest.objects.all().delete()

        second = self.ask(every_connection=True, idempotency_key="refresh-once")

        self.assertTrue(first["ok"], first)
        self.assertEqual(second, first)
        self.assertFalse(ReadRequest.objects.exists())

    def test_another_key_acts_again(self):
        self.ask(every_connection=True, idempotency_key="refresh-once")
        ReadRequest.objects.all().delete()

        self.ask(every_connection=True, idempotency_key="refresh-twice")

        self.assertEqual(ReadRequest.objects.count(), 1)

    def test_the_same_key_for_a_different_request_is_a_conflict(self):
        self.ask(every_connection=True, idempotency_key="refresh-once")

        result = self.ask(kinds=["tailscale.device"], idempotency_key="refresh-once")

        self.assertEqual(result["error"]["code"], "idempotency_conflict")

    def test_a_key_belongs_to_its_actor(self):
        self.ask(every_connection=True, idempotency_key="shared-text")
        ReadRequest.objects.all().delete()
        other = Principal("another-operator", "cli", cli_principal().capabilities)

        result = self.ask(principal=other, every_connection=True, idempotency_key="shared-text")

        self.assertTrue(result["ok"], result)
        self.assertEqual(ReadRequest.objects.count(), 1)

    def test_a_refused_request_leaves_its_key_free_for_the_corrected_one(self):
        refused = self.ask(connection_refs=["no-such-connection"], idempotency_key="refresh-once")
        self.assertEqual(refused["error"]["code"], "not_found")
        self.assertFalse(IdempotencyRecord.objects.exists())

        self.assertTrue(self.ask(every_connection=True, idempotency_key="refresh-once")["ok"])


class ReadTests(TestCase):
    def test_a_read_takes_no_key(self):
        result = execute_capability(
            "lookup.name.read",
            {"hostname": "example.com", "idempotency_key": "look-once"},
            principal=cli_principal(),
        )

        self.assertEqual(result["error"]["code"], "invalid_input")


class ReconcileTests(TestCase):
    def test_a_key_used_for_another_record_is_a_conflict(self):
        managing_everything()
        for key, address in (("example-rewrite", "192.0.2.10"), ("example-other", "192.0.2.11")):
            declare(key, "adguard.rewrite", {"domain": f"{key}.example.com", "answer": address})

        def ask(target):
            return execute_capability(
                RECONCILE, {"idempotency_key": "reconcile-once"}, principal=cli_principal(), target=target
            )

        ask("example-rewrite")
        other = ask("example-other")

        self.assertEqual(other["error"]["code"], "idempotency_conflict")
        self.assertEqual(OperationRequest.objects.count(), 1)


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
