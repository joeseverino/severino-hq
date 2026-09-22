"""Capability policy: what an operator decided must happen before a credential acts."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from control_plane.models import ApprovalRequest, CapabilityRule, ManagedResource
from core.models import AgentIdentity, AuditLog
from projects.models import Project

from .approvals import approve
from .capabilities import capability_label, capability_registry, execute_capability
from .capability_policy import Rule, Scope, decide, field_name, matrix, set_rule
from .security import AuthorizationError, Capability, Principal, cli_principal, web_principal
from .test_approvals import POLICY_KEY, declare_policy, policy_document, update_payload

WRITES = frozenset({Capability.READ, Capability.WRITE_PROJECTS, Capability.MANAGE_INFRASTRUCTURE})


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

    def test_an_ordinary_write_still_runs(self):
        result = execute_capability("project.create", {"name": "Runs"}, principal=agent())

        self.assertTrue(result["ok"])
        self.assertTrue(Project.objects.filter(name="Runs").exists())


class SurfaceRuleTests(PolicyTestCase):
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

        with self.assertRaisesMessage(Exception, "has changed since it was asked for"):
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
            ["mcp · project.delete: Default → Require approval",
             "mcp · project.delete: Require approval → Default"],
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
        with self.assertRaisesMessage(ValueError, "is a read"):
            self.set(spec=spec("contact.submissions.list"))

    def test_an_agent_rule_cannot_reach_past_its_pocket_id_grant(self):
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "write_projects"])

        with self.assertRaisesMessage(ValueError, "does not include project.delete"):
            self.set(scope=Scope.AGENT, subject="example-agent")

    def test_an_agent_rule_needs_an_agent_that_has_been_seen(self):
        with self.assertRaisesMessage(ValueError, "has presented a token here"):
            self.set(scope=Scope.AGENT, subject="nobody")


class PageTests(PolicyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        AgentIdentity.objects.create(
            client_id="example-agent", interfaces=["mcp"], granted=["read", "write_projects"]
        )

    def page(self):
        return self.client.get(reverse("agent_policy"))

    def test_it_shows_both_surfaces_and_every_agent_seen(self):
        page = self.page()

        for text in ("All MCP agents", "All API clients", "example-agent", "granted", "Projects", "Rules set", "Awaiting approval"):
            self.assertContains(page, text)

    def test_an_agent_cannot_be_offered_what_pocket_id_did_not_grant(self):
        page = self.page().content.decode()

        self.assertIn("Not granted", page)
        self.assertNotIn(f'name="{field_name(Scope.AGENT, "example-agent", "project.delete")}"', page)

    def test_a_read_offers_no_approval(self):
        columns, groups = matrix()
        read = next(row for group in groups for row in group.rows if row.effect == "read")
        options = {value for cell in read.cells for value, _ in cell.options}

        self.assertNotIn(Rule.APPROVE, options)

    def test_saving_a_change_records_it_and_marks_the_cell(self):
        field = field_name(Scope.SURFACE, "mcp", "project.delete")

        response = self.client.post(reverse("agent_policy"), {field: Rule.APPROVE}, follow=True)

        self.assertContains(response, "Saved 1 change")
        self.assertEqual(CapabilityRule.objects.get().rule, Rule.APPROVE)
        self.assertContains(response, "policy-chip rule-approve")
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

        self.assertContains(response, "does not include project.delete")
        self.assertFalse(CapabilityRule.objects.exists())

    def test_signed_out_it_cannot_be_reached(self):
        self.client.logout()

        self.assertEqual(self.page().status_code, 302)

    def test_reads_and_writes_on_one_thing_sit_together(self):
        _, groups = matrix()
        projects = next(group for group in groups if group.label == "Projects")
        actions = [row.label for row in projects.rows]

        self.assertIn("Create", actions)
        self.assertIn("Delete", actions)
        self.assertEqual([row.effect for row in projects.rows], sorted(
            (row.effect for row in projects.rows), key=["read", "remote_write", "destructive", "infrastructure_change"].index
        ))

    def test_no_two_groups_share_a_name(self):
        _, groups = matrix()
        labels = [group.label for group in groups]

        self.assertEqual(len(labels), len(set(labels)))

    def test_an_action_drops_the_words_its_group_and_prefix_already_say(self):
        from .capability_policy import _action

        self.assertEqual(_action("Example Widget Mark Ready", "Widgets", "example"), "Mark ready")
        self.assertEqual(_action("Project Create", "Projects", "project"), "Create")

    def test_only_acronyms_are_capitalised(self):
        self.assertEqual(capability_label("example.pace.set"), "Example Pace Set")
        self.assertEqual(capability_label("tls.certificate"), "TLS Certificate")

    def test_two_capabilities_that_shorten_alike_keep_their_prefix(self):
        from .capability_policy import _actions

        specs = [spec("documentation.sync"), spec("hq.sync")]

        self.assertEqual(_actions(specs, "Documentation", capability_label), ["Sync", "HQ sync"])

    def test_the_matrix_costs_the_same_however_many_capabilities_and_agents(self):
        AgentIdentity.objects.create(client_id="second-agent", granted=["read"])

        with self.assertNumQueries(3):
            matrix()
