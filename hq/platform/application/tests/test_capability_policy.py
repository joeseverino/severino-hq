"""Capability policy: what an operator decided must happen before a credential acts."""

from dataclasses import replace

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, ManagedResource
from hq.domains.control_plane.providers import PROVIDERS
from hq.domains.projects.models import Project
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.tests.markup import text
from hq.platform.core.models import AgentIdentity, AuditLog
from hq.platform.core.tests.estate_sizes import assert_cost_does_not_grow

from ..approvals import APPROVE, DELETING_FLAGS, ApproveCommand, approve
from ..capabilities import capability_registry, execute_capability, validate_command
from ..capability_policy import RULE_LABELS, Rule, decide, field_name, matrix, set_rule
from ..certificates import UPLOAD
from ..default_hold import needs_approval
from ..doors import interface_label
from ..entity_links import kind_label
from ..integration_specs import capability_schema
from ..labels import dotted, human_label, lower_first
from ..security import Capability, Principal, cli_principal, web_principal
from .test_approvals import POLICY_KEY, declare_policy, policy_document, update_payload
from .writers import keyed

WRITES = frozenset({Capability.READ, "write_projects", Capability.MANAGE_INFRASTRUCTURE})
DELETES = WRITES | {"delete_projects"}


def agent(actor="example-agent", interface="mcp", capabilities=WRITES):
    return Principal(actor, interface, capabilities, granted=frozenset(str(c) for c in capabilities))


def spec(name):
    return capability_registry()[name]


_EMPTY = {"array": [], "object": {}, "string": "", "boolean": False, "integer": 0, "number": 0}


def required_fields(registered):
    """The least payload a command accepts: each required field, empty."""

    schema = capability_schema(registered)
    return {field: _EMPTY[schema["properties"][field].get("type")] for field in schema.get("required", ())}


class PolicyTestCase(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("op", password="x" * 20)
        self.operator = web_principal(self.user)

    def rule(self, subject, capability, rule):
        CapabilityRule.objects.create(subject=subject, capability=capability, rule=rule)


class DefaultTests(PolicyTestCase):
    def test_a_gated_declaration_is_still_held(self):
        declare_policy()

        result = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(policy_document("group:elsewhere"))),
            principal=agent(),
            target=POLICY_KEY,
        )

        self.assertEqual(result["status"], "awaiting_approval")

    def test_an_agents_deleting_flag_waits_like_a_delete_however_it_is_spelled(self):
        """An orphan prune deletes every record the manifest leaves out, so an
        agent's is held though the capability only writes. Judged on the
        command, because validation reads 1 and "true" as True and a policy
        that read the payload would let either run a prune nobody approved."""

        from hq.domains.docs_index.models import DocumentationRecord

        DocumentationRecord.objects.create(doc_id="example-kept", title="Kept", obsidian_path="Kept.md")
        everything = agent(capabilities=frozenset(Capability))
        flagged = [
            (name, flag, set(capability_schema(registered)["properties"]))
            for name, registered in capability_registry().items()
            for flag in DELETING_FLAGS
            if flag in capability_schema(registered).get("properties", {})
        ]
        self.assertTrue(flagged, "no command declares a deleting flag, so the hold guards nothing")
        for name, flag, fields in flagged:
            plain = required_fields(spec(name))
            for spelling in (True, 1, "true"):
                with self.subTest(capability=name, spelling=spelling):
                    confirmed = {field: spelling for field in fields if field.startswith("confirm")}

                    result = execute_capability(
                        name, keyed({**plain, **confirmed, flag: spelling}), principal=everything
                    )

                    self.assertEqual(result.get("status"), "awaiting_approval", result)
                    self.assertTrue(DocumentationRecord.objects.filter(doc_id="example-kept").exists())
            self.assertTrue(execute_capability(name, keyed(plain), principal=everything)["ok"])

    def test_an_ordinary_write_still_runs(self):
        result = execute_capability("project.create", keyed({"name": "Runs"}), principal=agent())

        self.assertTrue(result["ok"])
        self.assertTrue(Project.objects.filter(name="Runs").exists())


class DestructiveDefaultTests(PolicyTestCase):
    """For an agent, anything destructive waits for a person unless a rule says otherwise."""

    def delete(self, principal, slug):
        return execute_capability("project.delete", keyed({"confirm": slug}), principal=principal, target=slug)

    def test_an_agents_delete_waits_and_happens_only_once_approved(self):
        project = Project.objects.create(name="Kept")

        held = self.delete(agent(capabilities=DELETES), project.slug)

        self.assertEqual(held["status"], "awaiting_approval")
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())
        approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_pausing_agents_also_stops_what_one_is_waiting_on(self):
        from ..agent_access import AgentAccessCommand, set_agents_paused
        from ..approvals import ApprovalError

        project = Project.objects.create(name="Kept")
        held = self.delete(agent(capabilities=DELETES), project.slug)

        set_agents_paused(AgentAccessCommand(paused=True), principal=self.operator)
        with self.assertRaises(ApprovalError):
            approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())

        set_agents_paused(AgentAccessCommand(paused=False), principal=self.operator)
        approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)
        self.assertFalse(Project.objects.filter(pk=project.pk).exists())

    def test_a_held_call_whose_subject_cannot_be_read_is_refused_not_run(self):
        """Nothing to show a person means nothing to approve, not no approval."""

        from unittest import mock

        from ..record_errors import RecordNotFound

        project = Project.objects.create(name="Unseen")
        with mock.patch("hq.platform.application.resources.get_resource", side_effect=RecordNotFound("x")):
            result = self.delete(agent(capabilities=DELETES), project.slug)

        self.assertEqual(result["error"]["code"], "approval_subject_unreadable")
        self.assertIn("HQ could not read what it would change", result["error"]["message"])
        self.assertNotIn("project.delete", result["error"]["message"])
        self.assertTrue(Project.objects.filter(pk=project.pk).exists())
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_an_explicit_allow_for_one_agent_lifts_it_for_that_agent_alone(self):
        self.rule("trusted-agent", "project.delete", Rule.ALLOW)
        first = Project.objects.create(name="First")
        second = Project.objects.create(name="Second")

        ran = self.delete(agent("trusted-agent", capabilities=DELETES), first.slug)
        held = self.delete(agent("other-agent", capabilities=DELETES), second.slug)

        self.assertTrue(ran["ok"])
        self.assertFalse(Project.objects.filter(pk=first.pk).exists())
        self.assertEqual(held["status"], "awaiting_approval")

    def test_the_operator_and_the_cli_are_not_held(self):
        for principal in (self.operator, cli_principal()):
            with self.subTest(principal=principal.interface):
                self.assertEqual(
                    decide(spec("project.delete"), principal, {}, "a-project").rule,
                    Rule.ALLOW,
                )


class AgentRuleTests(PolicyTestCase):
    """A rule names one agent and one action, over whichever surface it calls through."""

    def setUp(self):
        super().setUp()
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()

    def test_ask_me_first_holds_any_capability_and_applies_on_approval(self):
        self.rule("example-agent", "project.create", Rule.APPROVE)

        held = execute_capability("project.create", keyed({"name": "Held"}), principal=agent())

        self.assertEqual(held["status"], "awaiting_approval")
        self.assertFalse(Project.objects.filter(name="Held").exists())
        approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)
        self.assertTrue(Project.objects.filter(name="Held").exists())

    def test_a_held_change_to_a_record_goes_stale_if_the_record_moves(self):
        project = Project.objects.create(name="Original")
        self.rule("example-agent", "project.update", Rule.APPROVE)
        held = execute_capability("project.update", keyed({"name": "Renamed"}), principal=agent(), target=project.slug)
        Project.objects.filter(pk=project.pk).update(description="changed underneath")

        with self.assertRaisesMessage(Exception, "This changed after the agent asked"):
            approve(ApproveCommand(held["approval"]["id"]), principal=self.operator)
        self.assertEqual(
            ApprovalRequest.objects.get(pk=held["approval"]["id"]).state,
            ApprovalRequest.State.STALE,
        )

    def test_off_refuses_and_is_recorded_as_a_denial(self):
        self.rule("example-agent", "project.create", Rule.DENY)

        result = execute_capability("project.create", keyed({"name": "Nope"}), principal=agent())

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "denied_by_policy")
        row = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertEqual(row.metadata["reason"], "denied_by_policy")
        self.assertEqual(row.metadata["actor"], "example-agent")

    def test_on_over_a_held_declaration_is_consent_and_says_whose(self):
        declare_policy()
        self.rule("example-agent", "infrastructure.resource.update", Rule.ALLOW)

        result = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(policy_document("group:elsewhere"))),
            principal=agent(),
            target=POLICY_KEY,
        )

        self.assertNotEqual(result.get("status"), "awaiting_approval")
        self.assertEqual(
            ManagedResource.objects.get(key=POLICY_KEY).spec["document"],
            policy_document("group:elsewhere"),
        )
        updating = spec("infrastructure.resource.update")
        decision = decide(updating, agent(), validate_command(updating, update_payload("x")), POLICY_KEY)
        self.assertTrue(decision.overrides_a_hold)
        self.assertEqual(decision.source, "example-agent policy")

    def test_a_rule_tightens_for_that_agent_alone(self):
        self.rule("held-agent", "project.create", Rule.APPROVE)

        held = execute_capability("project.create", keyed({"name": "A"}), principal=agent("held-agent"))
        runs = execute_capability("project.create", keyed({"name": "B"}), principal=agent("free-agent"))

        self.assertEqual(held["status"], "awaiting_approval")
        self.assertTrue(runs["ok"])

    def test_a_rule_holds_on_every_surface_the_agent_calls_through(self):
        self.rule("example-agent", "project.create", Rule.DENY)

        for interface in ("mcp", "api"):
            with self.subTest(interface=interface):
                decision = decide(spec("project.create"), agent(interface=interface), {}, None)

                self.assertEqual((decision.rule, decision.source), (Rule.DENY, "example-agent policy"))

    def test_with_no_rule_the_agent_gets_the_default(self):
        self.rule("other-agent", "project.delete", Rule.ALLOW)

        decision = decide(spec("project.delete"), agent(capabilities=DELETES), {"confirm": "x"}, "x")

        self.assertEqual((decision.rule, decision.source), (Rule.APPROVE, "default"))


class OutOfScopeTests(PolicyTestCase):
    def test_the_operator_is_never_held_or_refused_by_policy(self):
        self.rule(self.user.get_username(), "project.create", Rule.DENY)

        self.assertEqual(decide(spec("project.create"), self.operator, {}, None).rule, Rule.ALLOW)

    def test_the_cli_keeps_todays_behavior(self):
        self.rule(cli_principal().actor, "project.create", Rule.DENY)

        self.assertEqual(decide(spec("project.create"), cli_principal(), {}, None).rule, Rule.ALLOW)


class SettingTests(PolicyTestCase):
    def setUp(self):
        super().setUp()
        AgentIdentity.objects.create(client_id="example-agent", granted=["read", "write_projects", "delete_projects"])

    def set(self, **overrides):
        arguments = {
            "subject": "example-agent",
            "spec": spec("project.delete"),
            "rule": Rule.APPROVE,
            "user": self.user,
        }
        return set_rule(**{**arguments, **overrides})

    def test_a_change_is_audited_with_both_sides(self):
        self.set()
        self.set(rule=None)

        messages = list(
            AuditLog.objects.filter(object_type="Capability policy").order_by("id").values_list("message", flat=True)
        )
        before, after = "Default", RULE_LABELS[Rule.APPROVE]
        self.assertEqual(len(messages), 2)
        for message, (old, new) in zip(messages, ((before, after), (after, before)), strict=True):
            self.assertIn("project.delete", message)
            self.assertLess(message.index(old), message.index(new))
        self.assertFalse(CapabilityRule.objects.exists())

    def test_setting_what_already_holds_changes_and_records_nothing(self):
        self.set()

        self.assertFalse(self.set())
        self.assertEqual(AuditLog.objects.filter(object_type="Capability policy").count(), 1)

    def test_nothing_governed_by_policy_may_set_it(self):
        """Whatever its grant, and a person behind it or not: the policy is a person's in the browser."""

        everything = frozenset(Capability)
        cell = {"subject": "example-agent", "capability": "project.delete", "rule": Rule.ALLOW}
        for credential in (
            agent(capabilities=everything),
            replace(agent(capabilities=everything), person_key="example-key"),
            replace(agent(interface="api", capabilities=everything), person_key="example-key"),
            cli_principal(),
        ):
            with self.subTest(interface=credential.interface, person=credential.person_key):
                result = execute_capability("agent.policy.set", keyed({"rules": [cell]}), principal=credential)

                self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(CapabilityRule.objects.exists())

    def test_a_read_can_be_denied_but_not_held(self):
        with self.assertRaisesMessage(ValueError, "Look up a name only reads."):
            self.set(spec=spec("lookup.name.read"))

    def test_a_rule_cannot_reach_past_its_pocket_id_grant(self):
        AgentIdentity.objects.filter(client_id="example-agent").update(granted=["read", "write_projects"])

        with self.assertRaisesMessage(
            ValueError, "The sign-in service does not allow example-agent to use Delete a project."
        ):
            self.set()

    def test_a_persons_own_settings_take_a_rule_only_for_an_agent_a_person_signed_in(self):
        AgentIdentity.objects.create(client_id="example-machine", granted=["pin_favorites"])
        AgentIdentity.objects.create(client_id="example-assistant", granted=["pin_favorites"], person=self.user)

        self.assertTrue(self.set(subject="example-assistant", spec=spec("pin.toggle")))
        with self.assertRaisesMessage(ValueError, "example-machine signs in as itself"):
            self.set(subject="example-machine", spec=spec("pin.toggle"))
        _columns, groups = matrix()
        row = next(row for group in groups for row in group.rows if row.name == "pin.toggle")
        offered = {cell.column: bool(cell.options) for cell in row.cells}
        self.assertEqual((offered["example-assistant"], offered["example-machine"]), (True, False))

    def test_a_rule_needs_an_agent_that_has_been_seen(self):
        with self.assertRaisesMessage(ValueError, "No agent named nobody has connected yet."):
            self.set(subject="nobody")


class PageTests(PolicyTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)
        AgentIdentity.objects.create(client_id="example-agent", interfaces=["mcp"], granted=["read", "write_projects"])

    def page(self):
        # Every action: the page opens on the rules that differ, and here none do.
        return self.client.get(reverse("agent_policy"), {"all": 1})

    def test_it_shows_a_column_per_agent_seen_and_offers_only_what_was_granted(self):
        page = self.page()
        columns, _groups = matrix()

        self.assertEqual([column.subject for column in columns], ["example-agent"])
        self.assertContains(page, "example-agent")
        self.assertContains(page, f'name="{field_name("example-agent", "project.create")}"')
        self.assertIn("Not granted", text(page))
        self.assertNotContains(page, f'name="{field_name("example-agent", "project.delete")}"')

    def test_what_only_a_person_can_do_has_no_rule_for_agents(self):
        _columns, groups = matrix()
        shown = {row.name for group in groups for row in group.rows}

        self.assertNotIn(APPROVE, shown)
        self.assertNotIn(UPLOAD, shown)
        self.assertIn("infrastructure.resource.reconcile", shown)
        with self.assertRaises(ValueError):
            set_rule(
                subject="example-agent",
                spec=spec(APPROVE),
                rule=Rule.ALLOW,
                user=self.user,
            )
        self.assertFalse(CapabilityRule.objects.exists())

    def test_a_read_offers_no_approval(self):
        _columns, groups = matrix()
        read = next(row for group in groups for row in group.rows if row.effect == "read")
        options = {value for cell in read.cells for value, _ in cell.options}

        self.assertNotIn(Rule.APPROVE, options)

    def test_saving_a_change_records_it_and_marks_the_cell(self):
        field = field_name("example-agent", "project.create")

        response = self.client.post(reverse("agent_policy"), form_keyed({field: Rule.APPROVE}), follow=True)

        self.assertContains(response, "Saved 1 change")
        self.assertEqual(CapabilityRule.objects.get().rule, Rule.APPROVE)
        _columns, groups = matrix()
        cell = next(cell for group in groups for row in group.rows for cell in row.cells if cell.field == field)
        self.assertEqual(cell.rule, Rule.APPROVE)

    def test_saving_an_unchanged_page_records_nothing(self):
        response = self.client.post(
            reverse("agent_policy"), form_keyed({field_name("example-agent", "project.create"): ""}), follow=True
        )

        self.assertContains(response, "Nothing changed.")
        self.assertFalse(AuditLog.objects.filter(object_type="Capability policy").exists())

    def test_a_forged_rule_past_the_grant_is_refused_in_words(self):
        field = field_name("example-agent", "project.delete")

        response = self.client.post(reverse("agent_policy"), form_keyed({field: Rule.ALLOW}), follow=True)

        self.assertIn("The sign-in service does not allow example-agent to use Delete a project.", text(response))
        self.assertFalse(CapabilityRule.objects.exists())

    def test_signed_out_it_cannot_be_reached(self):
        self.client.logout()

        self.assertEqual(self.page().status_code, 302)

    def test_reads_and_writes_on_one_thing_sit_together(self):
        _, groups = matrix()
        projects = next(group for group in groups if group.label == "Projects")
        actions = [row.label for row in projects.rows]

        self.assertIn("Add a project", actions)
        self.assertIn("Delete a project", actions)
        self.assertEqual(
            [row.effect for row in projects.rows],
            sorted(
                (row.effect for row in projects.rows),
                key=["read", "remote_write", "destructive", "infrastructure_change"].index,
            ),
        )

    def test_no_two_groups_share_a_name(self):
        _, groups = matrix()
        labels = [group.label for group in groups]

        self.assertEqual(len(labels), len(set(labels)))

    def test_a_row_is_the_commands_whole_title_and_says_what_it_does(self):
        _, groups = matrix()
        rows = {row.name: row for group in groups for row in group.rows}

        for name, row in rows.items():
            with self.subTest(name):
                self.assertEqual(row.label, spec(name).title)
                self.assertEqual(bool(row.effect_label), row.effect != "read")
        self.assertNotEqual(rows["project.delete"].effect_label, rows["project.create"].effect_label)

    def test_a_default_says_what_happens_in_the_words_the_choices_use(self):
        _, groups = matrix()
        rows = {row.name: row for group in groups for row in group.rows}
        choices = {label for row in rows.values() for cell in row.cells for _value, label in cell.options}

        self.assertEqual(rows["project.delete"].default, RULE_LABELS[Rule.APPROVE])
        self.assertEqual(rows["project.create"].default, RULE_LABELS[Rule.ALLOW])
        named = f"{RULE_LABELS[Rule.APPROVE]} for "
        self.assertTrue(rows["infrastructure.resource.update"].default.startswith(named))
        self.assertLessEqual({row.default for row in rows.values() if not row.default.startswith(named)}, choices)

    def test_a_type_waiting_for_approval_is_named_as_its_page_names_it(self):
        _, groups = matrix()
        default = {row.name: row.default for group in groups for row in group.rows}["infrastructure.resource.update"]
        held = [
            kind
            for kind, provider in PROVIDERS.items()
            if needs_approval(provider, spec("infrastructure.resource.update"))
        ]

        self.assertTrue(held)
        for kind in held:
            with self.subTest(kind):
                self.assertIn(lower_first(kind_label(kind)), default)

    def test_a_column_says_how_its_agent_comes_in_and_for_whom_as_written(self):
        AgentIdentity.objects.create(client_id="example-agent", person=self.user, person_key="op", interfaces=["mcp"])
        details = {column.label: column.detail for column in matrix()[0] if column.identity.person}

        self.assertEqual(details["example-agent"], dotted(interface_label("mcp"), "for op"))

    def test_only_acronyms_are_capitalized(self):
        self.assertEqual(human_label("example.pace.set"), "Example Pace Set")
        self.assertEqual(human_label("tls.certificate"), "TLS Certificate")

    def test_the_matrix_costs_the_same_however_many_agents(self):
        def arrange(count):
            for index in range(AgentIdentity.objects.count(), count):
                AgentIdentity.objects.create(client_id=f"agent-{index}", granted=["read"])
                CapabilityRule.objects.create(subject=f"agent-{index}", capability="project.create", rule=Rule.DENY)

        def agents(drawn):
            columns, _groups = drawn
            return len(columns)

        assert_cost_does_not_grow(self, sizes=(1, 8), arrange=arrange, draw=lambda _count: matrix(), rows=agents)


class CommandTitleTests(TestCase):
    """A command is called what it declares, wherever a person reads it."""

    def test_a_declared_label_is_the_title(self):
        from ..capabilities import capability_title

        for name in ("project.create", "contact.submission.delete"):
            self.assertEqual(capability_title(name), spec(name).label)

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
            validate_capability_spec(replace(spec("project.create"), label=" Create a project"))

    def test_a_command_type_without_a_schema_does_not_compose(self):
        from dataclasses import dataclass, replace

        from django.core.exceptions import ImproperlyConfigured

        from ..integration_validation import validate_capability_spec

        class Opaque:
            pass

        @dataclass(frozen=True)
        class Unschemable:
            held: Opaque

        with self.assertRaisesMessage(ImproperlyConfigured, "cannot emit JSON Schema"):
            validate_capability_spec(replace(spec("project.create"), command_type=Unschemable))

    def test_a_connection_offers_the_command_by_its_title_and_marks_destruction(self):
        from ..connection_catalog import _ability_state
        from ..connection_contracts import ConnectionAbility, ConnectionInstance
        from ..security import Capability, Principal

        ability = ConnectionAbility(
            "example.remove",
            "Remove",
            "Removes one.",
            "destructive",
            grant="coarse",
            capability="contact.submission.delete",
        )
        operator = Principal("operator", "web", frozenset({Capability.READ, Capability.MANAGE_CONTACTS}))

        state = _ability_state(
            ability,
            ConnectionInstance("one", "One", "example", "good", "Healthy"),
            operator,
        )

        self.assertEqual(state.action.label, spec("contact.submission.delete").title)
        self.assertEqual(state.action.effect, "destructive")
