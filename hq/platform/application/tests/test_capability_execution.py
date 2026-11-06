"""How a capability runs: who may call it, what it checks, and what it records."""

import json
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt
from hq.platform.core.models import AuditLog
from hq_sdk.capabilities import StrictCommand

from ..capabilities import CapabilitySpec, describe_capabilities, execute_capability
from ..documentation import sync_documentation
from ..projects import ProjectCommand, save_project
from ..security import AuthorizationError, Capability, Principal, agent_principal, cli_principal
from .writers import keyed


class CapabilityTests(TestCase):
    def test_plugin_strict_commands_execute_through_the_host(self):
        class ExampleCommand(StrictCommand):
            value: str = "works"

        spec = CapabilitySpec(
            "example.read",
            "Read an example.",
            "read",
            "example.read",
            ExampleCommand,
            lambda command, **kwargs: {"ok": True, "value": command.value},
        )
        principal = Principal("test", "test", frozenset({"example.read"}))

        with mock.patch(
            "hq.platform.application.capabilities.capability_registry",
            return_value={spec.name: spec},
        ):
            result = execute_capability(spec.name, {}, principal=principal)

        self.assertEqual(result, {"ok": True, "value": "works"})

    def test_registry_emits_stable_json_schemas_and_effects(self):
        first, second = (describe_capabilities(principal=cli_principal()) for _ in range(2))

        self.assertEqual(first, second)
        project = next(item for item in first["capabilities"] if item["name"] == "project.create")
        self.assertEqual(project["effect"], "remote_write")
        self.assertEqual(project["resource"], "projects")
        self.assertIn("name", project["input_schema"]["properties"])

    def test_an_agent_not_granted_a_write_is_refused_it(self):
        with self.assertRaisesRegex(AuthorizationError, "write_projects"):
            save_project(
                ProjectCommand(name="Denied", slug="denied"),
                principal=agent_principal("example-agent", "mcp", frozenset({"read"})),
            )
        self.assertFalse(Project.objects.filter(slug="denied").exists())

    def test_json_executor_returns_canonical_error_envelope(self):
        result = execute_capability(
            "project.create",
            {"slug": "missing-name"},
            principal=cli_principal(),
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")

    def test_a_target_of_the_wrong_kind_is_refused_by_name(self):
        """The message names the kind expected, so a client can correct itself.

        Reached only after authorization and payload validation have passed:
        a caller who may not run the capability is told that instead, and
        learns nothing about the shape of target it would have taken.
        """

        result = execute_capability(
            "expense.update",
            {
                "date": "2026-01-01",
                "vendor": "V",
                "item": "I",
                "total_cost": "1.00",
                "business_use_percentage": 100,
            },
            principal=cli_principal(),
            target="not-an-integer",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertIn("target must be a valid integer", result["error"]["message"])

    def test_an_unauthorized_caller_is_refused_before_its_target_is_read(self):
        result = execute_capability(
            "expense.update",
            {},
            principal=Principal("nobody", "test", frozenset()),
            target="not-an-integer",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")

    def test_json_executor_creates_through_same_service(self):
        result = execute_capability(
            "project.create",
            {"name": "JSON Project", "slug": "json-project"},
            principal=cli_principal(),
        )
        self.assertTrue(result["ok"])
        self.assertTrue(Project.objects.filter(slug="json-project").exists())

    def test_upsert_capability_is_idempotent_and_server_owned(self):
        created = execute_capability(
            "project.upsert",
            {"name": "First", "slug": "same-project"},
            principal=cli_principal(),
        )
        updated = execute_capability(
            "project.upsert",
            {"name": "Second", "slug": "same-project"},
            principal=cli_principal(),
        )

        self.assertTrue(created["created"])
        self.assertFalse(updated["created"])
        self.assertEqual(Project.objects.get().name, "Second")

    def test_hq_sync_refuses_a_payload_it_no_longer_understands(self):
        """A topology in the payload is refused: HQ derives its own."""

        result = execute_capability(
            "hq.sync",
            {
                "manifest": [
                    {
                        "doc_id": "rb-atomic-sync",
                        "title": "Atomic sync",
                        "doc_type": "runbook",
                        "status": "active",
                        "system": "Example service",
                        "environment": "other",
                        "sensitivity": "internal",
                    }
                ],
                "topology": {"version": 3},
            },
            principal=cli_principal(),
        )

        self.assertFalse(result["ok"])
        self.assertFalse(DocumentationRecord.objects.filter(doc_id="rb-atomic-sync").exists())

    def test_syncing_docs_does_not_require_the_whole_control_plane(self):
        """A service account allowed to sync docs and nothing else can.

        A documentation sync needs no control-plane write: no resource upsert,
        removal or reconcile.
        """

        docs_only = Principal("docs-bot", "mcp", frozenset({Capability.SYNC_DOCUMENTATION}))

        result = execute_capability(
            "hq.sync",
            keyed(
                {
                    "manifest": [
                        {
                            "doc_id": "rb-docs-only",
                            "title": "Docs only",
                            "doc_type": "runbook",
                            "status": "active",
                            "system": "Example service",
                            "environment": "other",
                            "sensitivity": "internal",
                        }
                    ]
                }
            ),
            principal=docs_only,
        )

        self.assertTrue(result["ok"], result)
        self.assertTrue(DocumentationRecord.objects.filter(doc_id="rb-docs-only").exists())

    def test_an_unknown_field_is_refused_by_name(self):
        """A misspelled field is named, not folded into "could not be executed".

        The generic failure hides handler text for a reason, but these names
        are the caller's own keys, and without them a typo cannot be found.
        """

        result = execute_capability(
            "hq.sync",
            {"manifest": [], "manifets": [], "topology": {}},
            principal=cli_principal(),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(
            result["error"]["message"],
            "hq.sync: manifets is not a known field; topology is not a known field.",
        )
        self.assertEqual(
            [(detail["type"], detail["loc"]) for detail in result["error"]["details"]],
            [("extra_forbidden", ["manifets"]), ("extra_forbidden", ["topology"])],
        )

    def _refusal(self, command_type, payload):
        """Run a synthetic command and return the error it was refused with."""

        spec = CapabilitySpec(
            "example.decide",
            "Decide an example.",
            "read",
            "example.read",
            command_type,
            lambda command, **kwargs: {"ok": True},
        )
        principal = Principal("test", "test", frozenset({"example.read"}))
        with mock.patch(
            "hq.platform.application.capabilities.capability_registry",
            return_value={spec.name: spec},
        ):
            result = execute_capability(spec.name, payload, principal=principal)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "invalid_input")
        return result["error"]

    def test_an_invalid_choice_names_the_allowed_ones(self):
        from typing import Literal

        class DecideCommand(StrictCommand):
            verdict: Literal["applies", "does_not_apply"]

        error = self._refusal(DecideCommand, {"verdict": "maybe"})

        self.assertEqual(
            error["message"],
            "example.decide: verdict must be one of applies, does_not_apply.",
        )
        self.assertEqual(
            error["details"],
            [
                {
                    "type": "literal_error",
                    "loc": ["verdict"],
                    "msg": "Input should be 'applies' or 'does_not_apply'",
                }
            ],
        )

    def test_a_missing_field_is_named(self):
        result = execute_capability("project.create", {"slug": "missing-name"}, principal=cli_principal())

        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(result["error"]["message"], "project.create: name is required.")

    def test_a_wrong_type_names_the_field_and_the_type(self):
        class CountCommand(StrictCommand):
            counts: list[int]

        error = self._refusal(CountCommand, {"counts": [1, "two"]})

        self.assertEqual(error["message"], "example.decide: counts[1] must be a valid integer.")

    def test_a_refusal_never_repeats_the_value_it_refused(self):
        """Field names and choices only: a payload can carry a secret."""

        from typing import Literal

        from pydantic import field_validator

        secret = "hunter2-not-for-logs"

        class SecretCommand(StrictCommand):
            verdict: Literal["applies", "does_not_apply"]
            token: str

            @field_validator("token")
            @classmethod
            def _check(cls, value):
                raise ValueError(f"{value} is not a token")

        error = self._refusal(SecretCommand, {"verdict": secret, "token": secret})

        self.assertNotIn(secret, json.dumps(error, default=repr))
        self.assertEqual(
            error["message"],
            "example.decide: verdict must be one of applies, does_not_apply; token is invalid.",
        )
        self.assertEqual(
            [detail["msg"] for detail in error["details"]],
            ["Input should be 'applies' or 'does_not_apply'", "Input is invalid"],
        )

    def test_a_domain_refusal_names_the_field_not_the_value(self):
        # An address only the model checks, past the command's own types.
        secret = "hunter2-not-an-address"

        result = execute_capability(
            "project.create",
            {"name": "Domain", "slug": "domain", "repository_url": secret},
            principal=cli_principal(),
        )

        self.assertEqual(result["error"]["code"], "invalid_input")
        self.assertEqual(result["error"]["details"], {"repository_url": ["invalid"]})
        self.assertNotIn(secret, json.dumps(result, default=repr))

    def test_delete_requires_exact_confirmation(self):
        project = Project.objects.create(name="Keep Me", slug="keep-me")

        result = execute_capability(
            "project.delete",
            {"confirm": "wrong-target"},
            principal=cli_principal(),
            target=project.slug,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "rejected")
        self.assertEqual(result["error"]["message"], "project.delete could not be executed.")
        self.assertNotIn("wrong-target", result["error"]["message"])
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

    def test_delete_honors_optimistic_concurrency_and_audits_success(self):
        project = Project.objects.create(name="Delete Me", slug="delete-me")
        stale_timestamp = project.updated_at.isoformat()
        project.name = "Changed elsewhere"
        project.save()

        stale = execute_capability(
            "project.delete",
            {"confirm": project.slug},
            principal=cli_principal(),
            target=project.slug,
            expected_updated_at=stale_timestamp,
        )
        self.assertFalse(stale["ok"])
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

        deleted = execute_capability(
            "project.delete",
            {"confirm": project.slug},
            principal=cli_principal(),
            target=project.slug,
            expected_updated_at=project.updated_at.isoformat(),
        )
        self.assertTrue(deleted["ok"])
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())
        event = AuditLog.objects.get(
            action=AuditLog.Action.DELETED,
            object_type="Project",
            object_id=str(project.pk),
        )
        self.assertEqual(event.metadata["operation"], "project.delete")
        self.assertEqual(event.metadata["interface"], "cli")

    def test_an_agent_granted_writes_is_not_granted_deletes(self):
        project = Project.objects.create(name="Protected", slug="protected")

        result = execute_capability(
            "project.delete",
            {"confirm": project.slug},
            principal=agent_principal("example-agent", "mcp", frozenset({"read", "write_projects"})),
            target=project.slug,
        )

        self.assertFalse(result["ok"])
        # A grant the identity provider could add, so the refusal names it.
        self.assertEqual(result["error"]["code"], "insufficient_scope")
        self.assertEqual(result["error"]["details"], {"scope": "delete_projects"})
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

    def call_as_operator(self, request):
        output = StringIO()
        with mock.patch("sys.stdin", StringIO(json.dumps(request))):
            call_command("hq_call", stdout=output)
        return json.loads(output.getvalue())

    def test_the_operator_reaches_every_tool_the_cli_uses_without_a_token(self):
        """The CLI's contract is the MCP tool surface, over the host shell."""

        catalog = describe_capabilities(principal=cli_principal())
        self.assertEqual(self.call_as_operator({"tool": "describe_capabilities"}), catalog)
        created = self.call_as_operator(
            {
                "tool": "execute_capability",
                "arguments": {"name": "project.create", "payload": {"name": "By hand", "slug": "by-hand"}},
            }
        )
        self.assertTrue(created["ok"])
        self.assertIn("orphan_projects", self.call_as_operator({"tool": "audit_registry"}))
        # The operator is never an MCP agent, and never left bound afterwards.
        from hq.platform.mcp.identity import current_principal

        with self.assertRaises(AuthorizationError):
            current_principal()

    def test_the_operator_call_refuses_a_malformed_request(self):
        from django.core.management.base import CommandError

        for raw in (
            "not json",
            "[]",
            '{"tool": 3}',
            '{"tool": "no_such_tool"}',
            '{"tool": "audit_registry", "arguments": []}',
        ):
            with (
                self.subTest(raw=raw),
                mock.patch("sys.stdin", StringIO(raw)),
                self.assertRaises(CommandError),
            ):
                call_command("hq_call", stdout=StringIO())

    def test_receipt_json_capability_updates_metadata_without_file_access(self):
        receipt = Receipt.objects.create(
            file="receipts/fixture.pdf",
            original_filename="fixture.pdf",
        )
        result = execute_capability(
            "receipt.update",
            {"vendor": "Updated Vendor", "amount": "42.50"},
            principal=cli_principal(),
            target=receipt.id,
        )

        self.assertTrue(result["ok"])
        receipt.refresh_from_db()
        self.assertEqual(receipt.vendor, "Updated Vendor")
        self.assertEqual(receipt.original_filename, "fixture.pdf")

    def test_prune_is_a_separate_capability(self):
        with self.assertRaisesRegex(AuthorizationError, "prune_documentation"):
            sync_documentation(
                [],
                principal=agent_principal("example-agent", "mcp", frozenset({"read", "sync_documentation"})),
                prune_orphans=True,
                confirm_prune=True,
            )


class DocumentationSyncCapabilityTests(SimpleTestCase):
    """`hq sync` must be grantable without handing over every other write."""

    def test_a_doc_sync_grant_holds_only_documentation_sync(self):
        from hq.platform.application.security import AuthorizationError, Capability, agent_principal

        principal = agent_principal("example-agent", "mcp", frozenset({"read", "sync_documentation"}))
        principal.require(Capability.SYNC_DOCUMENTATION)
        for withheld in (
            "write_expenses",
            "write_receipts",
            "write_projects",
            "write_assets",
            "write_content",
            "write_documentation",
        ):
            with self.assertRaises(AuthorizationError):
                principal.require(withheld)

    def test_record_writes_do_not_imply_doc_sync(self):
        from hq.platform.application.security import AuthorizationError, Capability, agent_principal

        with self.assertRaises(AuthorizationError):
            agent_principal(
                "example-agent", "mcp", frozenset({"read", "write_projects", "write_documentation"})
            ).require(Capability.SYNC_DOCUMENTATION)


class InfrastructureCapabilityTests(SimpleTestCase):
    """Declaring topology and requesting certificates are different powers."""

    def test_an_infrastructure_grant_does_not_hold_certificate_renewal(self):
        from hq.platform.application.security import AuthorizationError, Capability, agent_principal

        principal = agent_principal("example-agent", "mcp", frozenset({"read", "manage_infrastructure"}))
        principal.require(Capability.MANAGE_INFRASTRUCTURE)
        with self.assertRaises(AuthorizationError):
            principal.require(Capability.REQUEST_CERTIFICATE_RENEWAL)

    def test_certificate_renewal_is_grantable_on_its_own(self):
        from hq.platform.application.security import AuthorizationError, Capability, agent_principal

        principal = agent_principal("example-agent", "mcp", frozenset({"read", "request_certificate_renewal"}))
        principal.require(Capability.REQUEST_CERTIFICATE_RENEWAL)
        with self.assertRaises(AuthorizationError):
            principal.require(Capability.MANAGE_INFRASTRUCTURE)
