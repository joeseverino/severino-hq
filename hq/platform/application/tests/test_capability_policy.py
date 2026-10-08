"""Capability policy: what an operator decided must happen before a credential acts."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, ManagedResource
from hq.domains.projects.models import Project
from hq.platform.core.models import AgentIdentity, AuditLog

from ..approvals import approve
from ..capabilities import capability_registry, execute_capability
from ..capability_policy import Rule, Scope, decide, field_name, matrix, set_rule
from ..labels import human_label
from ..security import AuthorizationError, Capability, Principal, cli_principal, web_principal
from .test_approvals import POLICY_KEY, declare_policy, policy_document, update_payload

WRITES = frozenset({Capability.READ, "write_projects", Capability.MANAGE_INFRASTRUCTURE})
DELETES = WRITES | {"delete_projects"}


def agent(actor="example-agent", interface="mcp", capabilities=WRITES):
    return Principal(actor, interface, capabilities, granted=frozenset(str(c) for c in capabilities))


def spec(name):
    return capability_registry()[name]


class PolicyTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.operator = web_principal(self.user)

    def rule(self, scope, subject, capability, rule):
        CapabilityRule.objects.create(scope=scope, subject=subject, capability=capability, rule=rule)


class DefaultTests(PolicyTestCase):
    def test_a_gated_declaration_is_still_held(self):
        declare_policy()

        result = execute_capability(
            "infrastructure.resource.update",
            update_payload(policy_document("group:elsewhere")),
            principal=agent(),
            target=POLICY_KEY,
        )

        self.assertEqual(result["status"], "awaiting_approval")

    def test_an_agents_orphan_prune_waits_like_a_delete(self):
        """A sync that prunes deletes every record the manifest leaves out,
        so an agent's is held, though the capability itself only writes."""

        from hq.domains.docs_index.models import DocumentationRecord

        DocumentationRecord.objects.create(doc_id="example-kept", title="Kept", obsidian_path="Kept.md")
        syncer = agent(capabilities=frozenset({
            Capability.READ, Capability.SYNC_DOCUMENTATION, Capability.PRUNE_DOCUMENTATION,
        }))

        pruning = execute_capability(
            "documentation.sync",
            {"manifest": [], "prune_orphans": True, "confirm_prune": True},
            principal=syncer,
        )
        plain = execute_capability("documentation.sync", {"manifest": []}, principal=syncer)

        self.assertEqual(pruning["status"], "awaiting_approval")
        self.assertTrue(DocumentationRecord.objects.filter(doc_id="example-kept").exists())
        self.assertTrue(plain["ok"])

    def test_an_ordinary_write_still_runs(self):
        result = execute_capability("project.create", {"name": "Runs"}, principal=agent())

        self.assertTrue(result["ok"])
        self.assertTrue(Project.objects.filter(name="Runs").exists())


class DestructiveDefaultTests(PolicyTestCase):
    """For an agent, anything destructive waits for a person unless a rule says otherwise."""

    def delete(self, principal, slug):
        return execute_capability(
            "project.delete", {"confirm": slug}, principal=principal, target=slug
        )

    def test_an_agents_delete_waits_and_happens_only_once_approved(self):
        project = Project.objects.create(name="Kept")

        held = self.delete(agent(capabilities=DELETES), project.slug)

        self.assertEqual(held["status"], "awaiting_approval")
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())
        approve(held["approval"]["id"], principal=self.operator)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_pausing_agents_also_stops_what_one_is_waiting_on(self):
        from ..agent_access import set_agents_paused
        from ..approvals import ApprovalError

        project = Project.objects.create(name="Kept")
        held = self.delete(agent(capabilities=DELETES), project.slug)

        set_agents_paused(True, principal=self.operator, user=self.user)
        with self.assertRaisesMessage(ApprovalError, "Agents are paused"):
            approve(held["approval"]["id"], principal=self.operator)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

        set_agents_paused(False, principal=self.operator, user=self.user)
        approve(held["approval"]["id"], principal=self.operator)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_a_held_call_whose_subject_cannot_be_read_is_refused_not_run(self):
        """Nothing to show a person means nothing to approve, not no approval."""

        from unittest import mock

        from ..resources import ResourceNotFound

        project = Project.objects.create(name="Unseen")
        with mock.patch("hq.platform.application.resources.get_resource", side_effect=ResourceNotFound("x")):
            result = self.delete(agent(capabilities=DELETES), project.slug)

        self.assertEqual(result["error"]["code"], "approval_subject_unreadable")
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_an_explicit_allow_for_one_agent_lifts_it_for_that_agent_alone(self):
        self.rule(Scope.AGENT, "trusted-agent", "project.delete", Rule.ALLOW)
        first = Project.objects.create(name="First")
        second = Project.objects.create(name="Second")

        ran = self.delete(agent("trusted-agent", capabilities=DELETES), first.slug)
        held = self.delete(agent("other-agent", capabilities=DELETES), second.slug)

        self.assertTrue(ran["ok"])
        self.assertFalse(Project.objects.filter(pk=first.pk).exists())
        self.assertEqual(held["status"], "awaiting_approval")

    def test_an_agent_allow_still_cannot_loosen_an_explicit_surface_rule(self):
        self.rule(Scope.SURFACE, "mcp", "project.delete", Rule.DENY)
        self.rule(Scope.AGENT, "trusted-agent", "project.delete", Rule.ALLOW)
        project = Project.objects.create(name="Stays")

        result = self.delete(agent("trusted-agent", capabilities=DELETES), project.slug)

        self.assertEqual(result["error"]["code"], "denied_by_policy")

    def test_the_operator_and_the_cli_are_not_held(self):
        for principal in (self.operator, cli_principal()):
            with self.subTest(principal=principal.interface):
                self.assertEqual(
                    decide(spec("project.delete"), principal, {}, "a-project").rule,
                    Rule.ALLOW,
                )


class SurfaceRuleTests(PolicyTestCase):
    def setUp(self):
        super().setUp()
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()

    def test_require_approval_holds_any_capability_and_applies_on_approval(self):
        self.rule(Scope.SURFACE, "mcp", "project.create", Rule.APPROVE)

        held = execute_capability("project.create", {"name": "Held"}, principal=agent())

        self.assertEqual(held["status"], "awaiting_approval")
        self.assertFalse(Project.objects.filter(name="Held").exists())
        approve(held["approval"]["id"], principal=self.operator)
        self.assertTrue(Project.objects.filter(name="Held").exists())

    def test_a_held_change_to_a_record_goes_stale_if_the_record_moves(self):
        project = Project.objects.create(name="Original")
        self.rule(Scope.SURFACE, "mcp", "project.update", Rule.APPROVE)
        held = execute_capability(
            "project.update", {"name": "Renamed"}, principal=agent(), target=project.slug
        )
        Project.objects.filter(pk=project.pk).update(description="changed underneath")

        with self.assertRaisesMessage(Exception, "This changed after the agent asked"):
            approve(held["approval"]["id"], principal=self.operator)
        self.assertEqual(
            ApprovalRequest.objects.get(pk=held["approval"]["id"]).state,
            ApprovalRequest.State.STALE,
        )

    def test_deny_refuses_and_is_recorded_as_a_denial(self):
        self.rule(Scope.SURFACE, "mcp", "project.create", Rule.DENY)

        result = execute_capability("project.create", {"name": "Nope"}, principal=agent())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "denied_by_policy")
        row = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertEqual(row.metadata["reason"], "denied_by_policy")
        self.assertEqual(row.metadata["actor"], "example-agent")

    def test_allowing_a_gated_declaration_is_consent_and_says_whose(self):
        declare_policy()
        self.rule(Scope.SURFACE, "mcp", "infrastructure.resource.update", Rule.ALLOW)

        result = execute_capability(
            "infrastructure.resource.update",
            update_payload(policy_document("group:elsewhere")),
            principal=agent(),
            target=POLICY_KEY,
        )

        self.assertNotEqual(result.get("status"), "awaiting_approval")
        self.assertEqual(
            ManagedResource.objects.get(key=POLICY_KEY).spec["document"],
            policy_document("group:elsewhere"),
        )
        decision = decide(
            spec("infrastructure.resource.update"), agent(), update_payload("x"), POLICY_KEY
        )
        self.assertTrue(decision.overrides_a_hold)
        self.assertEqual(decision.source, "mcp policy")

    def test_a_surface_rule_binds_only_its_surface(self):
        self.rule(Scope.SURFACE, "mcp", "project.create", Rule.DENY)

        result = execute_capability(
            "project.create", {"name": "Over the API"}, principal=agent(interface="api")
        )

        self.assertTrue(result["ok"])


class AgentRuleTests(PolicyTestCase):
    def test_an_agent_rule_tightens_for_that_agent_alone(self):
        self.rule(Scope.AGENT, "held-agent", "project.create", Rule.APPROVE)

        held = execute_capability("project.create", {"name": "A"}, principal=agent("held-agent"))
        runs = execute_capability("project.create", {"name": "B"}, principal=agent("free-agent"))

        self.assertEqual(held["status"], "awaiting_approval")
        self.assertTrue(runs["ok"])

    def test_an_agent_rule_cannot_loosen_its_surface(self):
        self.rule(Scope.SURFACE, "mcp", "project.create", Rule.DENY)
        self.rule(Scope.AGENT, "example-agent", "project.create", Rule.ALLOW)

        result = execute_capability("project.create", {"name": "Nope"}, principal=agent())

        self.assertEqual(result["error"]["code"], "denied_by_policy")


class OutOfScopeTests(PolicyTestCase):
    def test_the_operator_is_never_held_or_refused_by_policy(self):
        self.rule(Scope.SURFACE, "mcp", "project.create", Rule.DENY)

        self.assertEqual(
            decide(spec("project.create"), self.operator, {}, None).rule, Rule.ALLOW
        )

    def test_the_cli_keeps_todays_behaviour(self):
        self.rule(Scope.SURFACE, "mcp", "project.create", Rule.DENY)

        self.assertEqual(decide(spec("project.create"), cli_principal(), {}, None).rule, Rule.ALLOW)


class SettingTests(PolicyTestCase):
    def set(self, **overrides):
        arguments = {
            "scope": Scope.SURFACE,
            "subject": "mcp",
            "spec": spec("project.delete"),
            "rule": Rule.APPROVE,
            "principal": self.operator,
            "user": self.user,
        }
        return set_rule(**{**arguments, **overrides})

    def test_a_change_is_audited_with_both_sides(self):
        self.set()
        self.set(rule=None)

        messages = list(
            AuditLog.objects.filter(object_type="Capability policy")
            .order_by("id")
            .values_list("message", flat=True)
        )
        self.assertEqual(
            messages,
            ["mcp · project.delete: Default → Ask me first",
             "mcp · project.delete: Ask me first → Default"],
        )
        self.assertFalse(CapabilityRule.objects.exists())

    def test_setting_what_already_holds_changes_and_records_nothing(self):
        self.set()

        self.assertFalse(self.set())
        self.assertEqual(AuditLog.objects.filter(object_type="Capability policy").count(), 1)

    def test_nothing_governed_by_policy_may_set_it(self):
        for credential in (agent(), cli_principal()):
            with self.subTest(interface=credential.interface), self.assertRaises(AuthorizationError):
                self.set(principal=credential)

    def test_a_read_can_be_denied_but_not_held(self):
        with self.assertRaisesMessage(ValueError, "List messages only reads."):
            self.set(spec=spec("contact.submissions.list"))

    def test_an_agent_rule_cannot_reach_past_its_pocket_id_grant(self):
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "write_projects"])

        with self.assertRaisesMessage(ValueError, "Pocket ID does not allow example-agent to use Delete project."):
            self.set(scope=Scope.AGENT, subject="example-agent")

    def test_an_agent_rule_needs_an_agent_that_has_been_seen(self):
        with self.assertRaisesMessage(ValueError, "No agent named nobody has connected yet."):
            self.set(scope=Scope.AGENT, subject="nobody")


class PageTests(PolicyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        AgentIdentity.objects.create(
            client_id="example-agent", interfaces=["mcp"], granted=["read", "write_projects"]
        )

    def page(self):
        # Every action: the page opens on the rules that differ, and here none do.
        return self.client.get(reverse("agent_policy"), {"all": 1})

    def test_it_shows_both_surfaces_and_every_agent_seen(self):
        page = self.page()

        for text in ("All MCP agents", "All API clients", "example-agent", "permissions from Pocket ID",
                     "Projects", "0 rules changed from the default", "Agents are allowed"):
            self.assertContains(page, text)
        # Nothing waits, so nothing says how many wait.
        self.assertNotContains(page, "waiting for your approval")

    def test_an_agent_cannot_be_offered_what_pocket_id_did_not_grant(self):
        page = self.page().content.decode()

        self.assertIn("Pocket ID does not allow this", page)
        self.assertNotIn(f'name="{field_name(Scope.AGENT, "example-agent", "project.delete")}"', page)

    def test_a_read_offers_no_approval(self):
        _columns, groups = matrix()
        read = next(row for group in groups for row in group.rows if row.effect == "read")
        options = {value for cell in read.cells for value, _ in cell.options}

        self.assertNotIn(Rule.APPROVE, options)

    def test_saving_a_change_records_it_and_marks_the_cell(self):
        field = field_name(Scope.SURFACE, "mcp", "project.delete")

        response = self.client.post(reverse("agent_policy"), {field: Rule.APPROVE}, follow=True)

        self.assertContains(response, "Saved 1 change")
        self.assertEqual(CapabilityRule.objects.get().rule, Rule.APPROVE)
        self.assertContains(response, "policy-select rule-approve")
        # Deletes are off for MCP in this deployment: the rule is armed for
        # the day they are switched on, and the page says so.
        self.assertContains(response, "is-dormant")

    def test_saving_an_unchanged_page_records_nothing(self):
        response = self.client.post(
            reverse("agent_policy"), {field_name(Scope.SURFACE, "mcp", "project.delete"): ""}, follow=True
        )

        self.assertContains(response, "Nothing changed.")
        self.assertFalse(AuditLog.objects.filter(object_type="Capability policy").exists())

    def test_a_forged_rule_past_the_grant_is_refused_in_words(self):
        field = field_name(Scope.AGENT, "example-agent", "project.delete")

        response = self.client.post(reverse("agent_policy"), {field: Rule.ALLOW}, follow=True)

        self.assertContains(response, "Pocket ID does not allow example-agent to use Delete project.")
        self.assertFalse(CapabilityRule.objects.exists())

    def test_signed_out_it_cannot_be_reached(self):
        self.client.logout()

        self.assertEqual(self.page().status_code, 302)

    def test_reads_and_writes_on_one_thing_sit_together(self):
        _, groups = matrix()
        projects = next(group for group in groups if group.label == "Projects")
        actions = [row.label for row in projects.rows]

        self.assertIn("Create project", actions)
        self.assertIn("Delete project", actions)
        self.assertEqual([row.effect for row in projects.rows], sorted(
            (row.effect for row in projects.rows), key=["read", "remote_write", "destructive", "infrastructure_change"].index
        ))

    def test_no_two_groups_share_a_name(self):
        _, groups = matrix()
        labels = [group.label for group in groups]

        self.assertEqual(len(labels), len(set(labels)))

    def test_a_row_is_the_commands_whole_title_and_says_what_it_does(self):
        _, groups = matrix()
        rows = {row.name: row for group in groups for row in group.rows}

        self.assertEqual(rows["hq.import"].label, "Import projects and assets")
        self.assertEqual(rows["documentation.sync"].label, "Sync documentation")
        self.assertEqual(rows["project.delete"].effect_label, "Deletes")
        self.assertEqual(rows["project.create"].effect_label, "Changes HQ")
        self.assertEqual(rows["contact.submissions.list"].effect_label, "")

    def test_a_default_says_what_happens_in_the_words_the_choices_use(self):
        _, groups = matrix()
        rows = {row.name: row for group in groups for row in group.rows}

        self.assertEqual(rows["project.delete"].default, "Ask me first")
        self.assertEqual(rows["project.create"].default, "Allow")
        self.assertTrue(rows["infrastructure.resource.update"].default.startswith("Ask me first for "))
        self.assertNotIn("gated", rows["infrastructure.resource.update"].default)

    def test_only_acronyms_are_capitalised(self):
        self.assertEqual(human_label("example.pace.set"), "Example Pace Set")
        self.assertEqual(human_label("tls.certificate"), "TLS Certificate")


    def test_the_matrix_costs_the_same_however_many_capabilities_and_agents(self):
        AgentIdentity.objects.create(client_id="second-agent", granted=["read"])

        with self.assertNumQueries(3):
            matrix()


class CommandTitleTests(TestCase):
    """A command is called what it declares, wherever a person reads it."""

    def test_a_declared_label_is_the_title(self):
        from ..capabilities import capability_title

        self.assertEqual(spec("project.create").title, "Create project")
        self.assertEqual(capability_title("contact.submission.delete"), "Delete a message")

    def test_a_command_with_no_label_reads_as_its_name(self):
        from ..capabilities import capability_title

        self.assertEqual(capability_title("example.not.registered"), "Example Not Registered")

    def test_every_core_command_is_named(self):
        from ..core_capabilities import CORE_CAPABILITY_SPECS

        self.assertEqual([item.name for item in CORE_CAPABILITY_SPECS if not item.label], [])

    def test_a_padded_label_does_not_compose(self):
        from dataclasses import replace

        from django.core.exceptions import ImproperlyConfigured

        from ..integration_validation import validate_capability_spec

        with self.assertRaises(ImproperlyConfigured):
            validate_capability_spec(replace(spec("project.create"), label=" Create project"))

    def test_a_connection_offers_the_command_by_its_title_and_marks_destruction(self):
        from ..connection_catalog import _ability_state
        from ..connection_contracts import ConnectionAbility, ConnectionInstance
        from ..security import Capability, Principal

        ability = ConnectionAbility(
            "example.remove", "Remove", "Removes one.", "destructive",
            grant="coarse", capability="contact.submission.delete",
        )
        operator = Principal(
            "operator", "web", frozenset({Capability.READ, Capability.MANAGE_CONTACTS})
        )

        state = _ability_state(
            ability,
            ConnectionInstance("one", "One", "example", "good", "Healthy"),
            operator,
        )

        self.assertEqual(state.action.label, "Delete a message")
        self.assertEqual(state.action.effect, "destructive")
