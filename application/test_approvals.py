"""What the approval gate promises, written as the promises.

Each test is one sentence about the incident that produced this module: a
credential, on its own, changed the estate's access policy and the change reached
the live network within the minute. The properties below are what has to be true
for that not to be possible again, and each one is here because its absence is
exactly how the gate would fail quietly rather than loudly.
"""

from __future__ import annotations

from dataclasses import replace
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from control_plane.models import ApprovalRequest, ManagedResource, OperationRequest
from control_plane.providers import PROVIDERS

from .approvals import (
    ApprovalError,
    MAX_PENDING_PER_ACTOR,
    approve,
    compare,
    pending,
    preview,
    reject,
)
from .capabilities import execute_capability
from .controller import claim_next_operation, schedule_automatic_operations
from .infrastructure import ManagedResourceCommand, save_managed_resource
from .security import (
    AuthorizationError,
    Capability,
    Principal,
    cli_principal,
    mcp_principal,
    web_principal,
)


POLICY_KEY = "access-policy"
GATED_KIND = "tailscale.policy"
UNGATED_KIND = "adguard.rewrite"


def policy_document(who: str = "group:example") -> str:
    """A neutral access policy, as a document rather than as fields."""

    return json.dumps(
        {
            "tagOwners": {"tag:example": ["autogroup:admin"]},
            "grants": [{"src": [who], "dst": ["tag:example"], "ip": ["*"]}],
        },
        indent=2,
    )


def declare_policy(document: str = "") -> ManagedResource:
    """The declaration as it exists before anything asks to change it.

    Adopted rather than authored, which is how the real one arrives: the sweep
    reads the live policy and records it, so the declaration starts equal to the
    world. It is also the one write of a gated kind that needs no approval, for
    exactly that reason.
    """

    save_managed_resource(
        ManagedResourceCommand(
            key=POLICY_KEY,
            kind=GATED_KIND,
            spec={"document": document or policy_document()},
        ),
        principal=cli_principal(),
        copied_from_live=True,
    )
    return ManagedResource.objects.get(key=POLICY_KEY)


def update_payload(document: str) -> dict:
    return {
        "key": POLICY_KEY,
        "kind": GATED_KIND,
        "spec": {"document": document},
        "enabled": True,
    }


def reconcile_payload(key: str = "once") -> dict:
    return {"idempotency_key": key, "reason": "Stated reason for the change."}


def an_operator(username: str = "operator") -> Principal:
    user = get_user_model().objects.create_user(
        username=username, password="test-only-password"
    )
    return web_principal(user)


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class HeldRequestTests(TestCase):
    """A credential may ask. Asking is all it may do."""

    def setUp(self):
        self.resource = declare_policy()

    def test_a_reconcile_asked_for_over_a_token_waits_instead_of_queueing(self):
        result = execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        # Not an error: the caller asked for something legitimate and the answer
        # is that a person has to agree. An error reads as something to retry.
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "awaiting_approval")
        self.assertFalse(result["queued"])
        self.assertIn("approval", result)
        self.assertEqual(result["approval"]["resource_key"], POLICY_KEY)
        self.assertEqual(result["approval"]["requested_interface"], "mcp")
        # Nothing was queued, so there is nothing for a controller to find.
        self.assertFalse(OperationRequest.objects.exists())

    def test_an_unapproved_request_cannot_be_claimed_because_it_is_not_work_yet(self):
        execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        claimed = claim_next_operation(
            "controller-example", capabilities=((GATED_KIND, "reconcile"),)
        )

        self.assertIsNone(claimed["operation"])

    def test_amending_the_declaration_writes_nothing_until_somebody_agrees(self):
        """The amendment is held, not applied and then held.

        This is the half that a gate on reconciliation alone would miss. A
        declaration is what the operator is shown and what a later reconcile
        pushes, so an unapproved amendment sitting in desired state is a trap:
        the next person to press Reconcile, for their own reasons, applies
        somebody else's document.
        """

        result = execute_capability(
            "infrastructure.resource.update",
            update_payload(policy_document("group:elsewhere")),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertEqual(result["status"], "awaiting_approval")
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spec["document"], policy_document())
        self.assertEqual(self.resource.generation, 1)

    def test_removing_the_declaration_is_held_like_any_other_change(self):
        """Removal is the way round a gate on amendment and reconciliation.

        A declaration HQ has stopped keeping is a declaration nothing asserts
        any more, which for an access policy means whatever is live stays live
        with nobody watching it.
        """

        result = execute_capability(
            "infrastructure.resource.remove",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertTrue(ManagedResource.objects.filter(key=POLICY_KEY).exists())

    def test_disabling_the_declaration_is_held_too(self):
        """Turning a declaration off is a change to what HQ asserts."""

        payload = update_payload(policy_document()) | {"enabled": False}

        result = execute_capability(
            "infrastructure.resource.update",
            payload,
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertEqual(result["status"], "awaiting_approval")
        self.resource.refresh_from_db()
        self.assertTrue(self.resource.enabled)

    def test_declaring_a_second_one_is_held_as_firmly_as_amending_the_first(self):
        """A new declaration of a gated kind is new work for the controller."""

        result = execute_capability(
            "infrastructure.resource.create",
            {
                "key": "another-policy",
                "kind": GATED_KIND,
                "spec": {"document": policy_document()},
            },
            principal=mcp_principal(),
        )

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertFalse(ManagedResource.objects.filter(key="another-policy").exists())

    def test_asking_the_same_thing_twice_is_one_decision(self):
        first = execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        second = execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertEqual(first["approval"]["id"], second["approval"]["id"])
        self.assertTrue(second["repeated"])
        self.assertEqual(ApprovalRequest.objects.count(), 1)

    def test_a_caller_cannot_bury_the_queue_it_is_waiting_on(self):
        for index in range(MAX_PENDING_PER_ACTOR):
            execute_capability(
                "infrastructure.resource.update",
                update_payload(policy_document(f"group:example{index}")),
                principal=mcp_principal(),
                target=POLICY_KEY,
            )

        refused = execute_capability(
            "infrastructure.resource.update",
            update_payload(policy_document("group:one-too-many")),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error"]["code"], "too_many_pending_approvals")
        self.assertEqual(ApprovalRequest.objects.count(), MAX_PENDING_PER_ACTOR)

    def test_reading_is_never_held(self):
        """Nothing about a read waits for anybody."""

        result = execute_capability(
            "contact.submissions.list",
            {"status": "new", "limit": 1},
            principal=Principal(
                "reader", "mcp", frozenset({Capability.MANAGE_CONTACTS})
            ),
        )

        self.assertNotEqual(result.get("status"), "awaiting_approval")

    def test_an_unauthorized_caller_is_refused_rather_than_held(self):
        """Authority first. A request nobody may make is not a decision for anybody."""

        result = execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=Principal("stranger", "mcp", frozenset()),
            target=POLICY_KEY,
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "forbidden")
        self.assertFalse(ApprovalRequest.objects.exists())

    def test_an_ungated_kind_is_unaffected(self):
        """The rule is the smallest one that closes the hole.

        Everything else a token does over this interface behaves exactly as it
        did, which is the property that keeps the gate worth having: a person who
        is asked about everything stops reading what they are asked.
        """

        ManagedResource.objects.create(
            key="example-rewrite",
            kind=UNGATED_KIND,
            spec={"domain": "example.test", "answer": "192.0.2.10"},
        )

        result = execute_capability(
            "infrastructure.resource.update",
            {
                "key": "example-rewrite",
                "kind": UNGATED_KIND,
                "spec": {"domain": "example.test", "answer": "192.0.2.11"},
            },
            principal=mcp_principal(),
            target="example-rewrite",
        )

        self.assertTrue(result["ok"])
        self.assertNotEqual(result.get("status"), "awaiting_approval")
        self.assertEqual(
            ManagedResource.objects.get(key="example-rewrite").spec["answer"],
            "192.0.2.11",
        )


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class OperatorRequestTests(TestCase):
    """A person at the web surface is the signal the gate is waiting for."""

    def setUp(self):
        self.resource = declare_policy()

    def test_the_same_request_from_the_web_interface_is_not_held(self):
        result = execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=an_operator(),
            target=POLICY_KEY,
        )

        self.assertTrue(result["queued"])
        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_an_interface_nobody_has_declared_interactive_is_held(self):
        """The allowlist is the safe direction.

        An adapter added later is a credential until somebody decides otherwise.
        The other order would hand a new interface a person's standing by nobody
        having thought about it.
        """

        result = execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=Principal(
                "some-new-adapter",
                "something-new",
                frozenset({Capability.MANAGE_INFRASTRUCTURE}),
            ),
            target=POLICY_KEY,
        )

        self.assertEqual(result["status"], "awaiting_approval")


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class DecisionTests(TestCase):
    """What a decision does, and what no decision can do."""

    def setUp(self):
        self.resource = declare_policy()

    def held(self, payload=None, capability="infrastructure.reconcile"):
        execute_capability(
            capability,
            payload or reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        return ApprovalRequest.objects.get()

    def test_approval_queues_the_work_and_the_controller_can_then_claim_it(self):
        held = self.held()

        approve(str(held.id), principal=an_operator())

        operation = OperationRequest.objects.get()
        self.assertEqual(operation.action, OperationRequest.Action.RECONCILE)
        # Who asked stays who asked, and who agreed is recorded beside it.
        self.assertEqual(operation.requested_interface, "mcp")
        self.assertEqual(operation.input["approved_by"], "operator")
        claimed = claim_next_operation(
            "controller-example", capabilities=((GATED_KIND, "reconcile"),)
        )
        self.assertEqual(claimed["operation"]["id"], str(operation.id))

    def test_approving_an_amendment_applies_exactly_the_document_held(self):
        wanted = policy_document("group:elsewhere")
        held = self.held(
            payload=update_payload(wanted),
            capability="infrastructure.resource.update",
        )

        approve(str(held.id), principal=an_operator())

        self.resource.refresh_from_db()
        self.assertEqual(self.resource.spec["document"], wanted)
        self.assertEqual(self.resource.generation, 2)

    def test_a_token_cannot_approve_its_own_request(self):
        held = self.held()

        with self.assertRaises(AuthorizationError):
            approve(str(held.id), principal=mcp_principal())

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.PENDING)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_token_holding_every_capability_still_cannot_approve(self):
        """Not a capability. Deliberately.

        A capability to approve is one more grant a compromised credential could
        turn out to have, and then the hold would be worth nothing. The check is
        the interface, which a token cannot acquire.
        """

        held = self.held()
        omnipotent = Principal(
            "well-equipped-token", "mcp", frozenset(Capability)
        )

        with self.assertRaises(AuthorizationError):
            approve(str(held.id), principal=omnipotent)

    def test_an_operator_who_shares_the_requesting_name_is_refused(self):
        held = self.held()

        with self.assertRaises(AuthorizationError):
            approve(str(held.id), principal=an_operator(held.requested_actor))

    def test_a_decision_cannot_be_taken_twice(self):
        held = self.held()
        approve(str(held.id), principal=an_operator())

        with self.assertRaises(ApprovalError):
            approve(str(held.id), principal=an_operator("second-operator"))

        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_rejecting_answers_the_caller_and_applies_nothing(self):
        held = self.held()

        reject(str(held.id), principal=an_operator(), note="Not wanted.")

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.REJECTED)
        self.assertEqual(held.decision_note, "Not wanted.")
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_declaration_that_moves_invalidates_the_approval(self):
        """An approval covers a comparison, not a resource.

        Without this the hold is a delayed blank cheque: ask for a reconcile of
        something harmless, wait for the amendment to land, and the click that
        was read as approving one thing applies another.
        """

        held = self.held()
        # Moved by an operator, which is the ordinary way it happens: somebody
        # edits the declaration while a request to apply it is still waiting.
        save_managed_resource(
            ManagedResourceCommand(
                key=POLICY_KEY,
                kind=GATED_KIND,
                spec={"document": policy_document("group:changed-underneath")},
            ),
            principal=an_operator("editor"),
            current_key=POLICY_KEY,
        )

        with self.assertRaises(ApprovalError):
            approve(str(held.id), principal=an_operator())

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.STALE)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_request_nobody_answers_lapses(self):
        held = self.held()
        held.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        held.save(update_fields=("expires_at",))

        self.assertEqual(pending(), ())
        with self.assertRaises(ApprovalError):
            approve(str(held.id), principal=an_operator())

        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.EXPIRED)
        self.assertFalse(OperationRequest.objects.exists())


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class ControllerTests(TestCase):
    """The controller is not the thing this gate is about.

    It holds no token anybody could steal off a laptop: it pulls work HQ has
    already decided on, and the work it schedules for itself is convergence
    toward a declaration a person has already agreed to. Holding that would stop
    the estate maintaining itself to protect against nothing.
    """

    def test_automatic_convergence_of_a_gated_kind_is_still_scheduled(self):
        gated_device = replace(PROVIDERS["tailscale.device"], requires_approval=True)
        ManagedResource.objects.create(
            key="example-device",
            kind="tailscale.device",
            spec={"name": "example-device", "key_expiry_disabled": True},
            generation=2,
            observed_generation=1,
        )

        with patch.dict(PROVIDERS, {"tailscale.device": gated_device}):
            scheduled = schedule_automatic_operations("controller-example")

        self.assertEqual(len(scheduled["scheduled"]), 1)
        operation = OperationRequest.objects.get()
        self.assertEqual(operation.requested_interface, "controller")
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)


class FloorTests(TestCase):
    """The rule again, under the boundary that normally answers it.

    The hold keys on a capability saying it acts on an infrastructure resource,
    which every capability in the registry does say. What it cannot see is a
    caller that reaches the use case without going through a capability at all --
    a surface added later, or an extension. So the use cases refuse it too, and
    these are the tests that this floor is really there.
    """

    def setUp(self):
        self.resource = declare_policy()

    def test_writing_a_gated_declaration_without_consent_is_refused(self):
        from .infrastructure import PolicyError

        with self.assertRaises(PolicyError):
            save_managed_resource(
                ManagedResourceCommand(
                    key=POLICY_KEY,
                    kind=GATED_KIND,
                    spec={"document": policy_document("group:elsewhere")},
                ),
                principal=Principal(
                    "a-token", "mcp", frozenset({Capability.MANAGE_INFRASTRUCTURE})
                ),
                current_key=POLICY_KEY,
            )

    def test_queueing_work_on_a_gated_declaration_without_consent_is_refused(self):
        from .infrastructure import OperationCommand, PolicyError, request_reconcile

        with self.assertRaises(PolicyError):
            request_reconcile(
                OperationCommand(idempotency_key="direct", reason=""),
                principal=Principal(
                    "a-token", "mcp", frozenset({Capability.MANAGE_INFRASTRUCTURE})
                ),
                current_key=POLICY_KEY,
            )

        self.assertFalse(OperationRequest.objects.exists())

    def test_adopting_what_the_provider_already_holds_is_not_a_change(self):
        """The one exemption, and it changes nothing by construction.

        A sweep records the policy exactly as the tailnet holds it. Refused, the
        declaration could never be adopted at all, and HQ would show the one
        control it most needs to show as unmanaged.
        """

        save_managed_resource(
            ManagedResourceCommand(
                key=POLICY_KEY,
                kind=GATED_KIND,
                spec={"document": policy_document("group:live")},
            ),
            principal=cli_principal(),
            current_key=POLICY_KEY,
            copied_from_live=True,
        )

        self.assertEqual(
            ManagedResource.objects.get(key=POLICY_KEY).spec["document"],
            policy_document("group:live"),
        )


class PreviewTests(TestCase):
    """What a person is shown, because that is what they are approving."""

    def test_a_document_is_compared_by_meaning_rather_than_as_two_walls_of_text(self):
        before = {"document": policy_document("group:example")}
        after = {"document": policy_document("group:elsewhere")}

        shown = compare(before, after, label="Declaration would change")

        paths = {row.path: (row.before, row.after) for row in shown.rows}
        self.assertEqual(
            paths["document.grants[0].src[0]"], ("group:example", "group:elsewhere")
        )
        # Everything that did not move stays out of it.
        self.assertEqual(len(shown.rows), 1)
        self.assertEqual(shown.lines, ())

    def test_content_that_will_not_parse_falls_back_to_a_line_diff(self):
        """A policy document may carry comments, and then it is text.

        Claiming a semantic diff of something that did not parse would be worse
        than a line diff: it would silently compare two strings and report one
        difference where there might be twenty.
        """

        before = {"document": "// a note\nfirst line\nsecond line"}
        after = {"document": "// a note\nfirst line\nchanged line"}

        shown = compare(before, after, label="Declaration would change")

        self.assertEqual(shown.rows, ())
        self.assertTrue(any(line.startswith("-second line") for line in shown.lines))
        self.assertTrue(any(line.startswith("+changed line") for line in shown.lines))

    @override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
    def test_applying_a_declaration_is_compared_against_what_is_live(self):
        """For a reconcile the question is what happens to the world.

        Compared against the declaration it would be an empty diff, which is
        true and useless: of course applying a declaration matches the
        declaration. What a person needs is the difference from the record that
        is actually out there.
        """

        resource = declare_policy()
        resource.status = {"document": policy_document("group:live")}
        resource.save(update_fields=("status",))
        execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        shown = preview(ApprovalRequest.objects.get())

        paths = {row.path: (row.before, row.after) for row in shown.rows}
        self.assertEqual(
            paths["document.grants[0].src[0]"], ("group:live", "group:example")
        )


@override_settings(SEVERINO_MCP_ENABLE_INFRASTRUCTURE=True)
class SurfaceTests(TestCase):
    """A held request that nobody can see is a held request nobody answers."""

    def setUp(self):
        declare_policy()
        self.user = get_user_model().objects.create_user(
            username="reviewer", password="test-only-password"
        )
        self.client.force_login(self.user)
        execute_capability(
            "infrastructure.resource.update",
            update_payload(policy_document("group:elsewhere")),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.held = ApprovalRequest.objects.get(
            capability="infrastructure.resource.update"
        )
        execute_capability(
            "infrastructure.reconcile",
            reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

    def _entry(self, held):
        """A held request's own audit entry, reached the way an agent's link is."""

        return self.client.get(
            reverse("core:approval_entry", kwargs={"approval_id": held.id}), follow=True
        )

    def test_the_queue_names_who_asked_the_reason_and_the_difference(self):
        reconcile = ApprovalRequest.objects.get(capability="infrastructure.reconcile")

        amendment = self._entry(self.held)
        self.assertContains(amendment, "mcp-service-account")
        # An amendment's command has no reason field; the diff is the reason.
        self.assertContains(amendment, "document.grants[0].src[0]")
        self.assertContains(self._entry(reconcile), "Stated reason for the change.")

    def test_a_held_record_change_shows_what_would_be_written(self):
        from control_plane.models import CapabilityRule

        CapabilityRule.objects.create(
            scope="surface", subject="mcp", capability="project.create", rule="approve"
        )
        writer = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.WRITE_PROJECTS}))
        held = execute_capability("project.create", {"name": "Held project"}, principal=writer)

        entry = self._entry(ApprovalRequest.objects.get(pk=held["approval"]["id"]))

        self.assertContains(entry, "Would be created")
        self.assertContains(entry, "Held project")
        self.assertContains(entry, "Create project")

    def test_the_awaiting_view_is_the_queue(self):
        page = self.client.get(f"{reverse('core:audit_list')}?awaiting=1")

        self.assertEqual(len(page.context["events"]), 2)
        self.assertContains(page, "Awaiting approval · 2")

    def test_a_decision_is_offered_only_while_the_request_waits(self):
        """This card sits on a permanent record. A button on something already
        settled would invite a decision that cannot be taken."""

        self.assertContains(self._entry(self.held), "Approve")

        reject(str(self.held.id), principal=web_principal(self.user))

        settled = self._entry(self.held)
        self.assertNotContains(settled, "Approve")
        self.assertContains(settled, "Rejected")

    def test_the_old_address_still_lands_on_the_queue(self):
        response = self.client.get(reverse("control_plane:approvals"))

        self.assertRedirects(response, f"{reverse('core:audit_list')}?awaiting=1")

    def test_the_resource_page_says_something_is_waiting(self):
        page = self.client.get(
            reverse("control_plane:detail", kwargs={"key": POLICY_KEY})
        )

        self.assertContains(page, "waiting for your approval")

    def test_approving_from_the_page_applies_it(self):
        response = self.client.post(
            reverse(
                "control_plane:approval_decision", kwargs={"approval_id": self.held.id}
            ),
            {"decision": "approve"},
        )

        self.assertRedirects(response, f"{reverse('core:audit_list')}?awaiting=1")
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.APPROVED)
        self.assertEqual(
            ManagedResource.objects.get(key=POLICY_KEY).spec["document"],
            policy_document("group:elsewhere"),
        )

    def test_the_page_needs_a_signed_in_operator(self):
        self.client.logout()

        page = self.client.get(f"{reverse('core:audit_list')}?awaiting=1")

        self.assertEqual(page.status_code, 302)

    def test_each_request_is_an_action_item_that_opens_its_audit_entry(self):
        from .domains import domain_attention_items

        entries = [entry for entry in domain_attention_items() if entry["item"].eyebrow == "Approval"]

        self.assertEqual(len(entries), 2)
        self.assertEqual({entry["source"] for entry in entries}, {"Audit"})
        self.assertIn(
            reverse("core:approval_entry", kwargs={"approval_id": self.held.id}),
            {entry["item"].url for entry in entries},
        )

    def test_it_can_be_decided_from_the_action_items_page(self):
        page = self.client.get(reverse("action_items"))
        approve = reverse(
            "control_plane:approval_decide",
            kwargs={"approval_id": self.held.id, "decision": "reject"},
        )
        self.assertContains(page, approve)

        response = self.client.post(approve, {"next": reverse("action_items")})

        self.assertRedirects(response, reverse("action_items"), fetch_redirect_response=False)
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.REJECTED)
