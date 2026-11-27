"""What the approval gate promises, written as the promises.

A credential on its own must not be able to change a gated kind. Each test is
one property that has to hold for that, and would fail quietly if it did not.
"""

import json

from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from hq.domains.control_plane.models import ApprovalRequest, ManagedResource
from hq.platform.core.testing import operator

from ..approvals import (
    MAX_PENDING_PER_ACTOR,
    ApproveCommand,
    RejectCommand,
    approve,
    pending,
    reject,
)
from ..capabilities import execute_capability
from ..infrastructure import save_adopted
from ..plugin_testing import form_keyed
from ..security import (
    AuthorizationError,
    Principal,
    cli_principal,
    mcp_principal,
    web_principal,
)
from .writers import keyed

POLICY_KEY = "access-policy"
GATED_KIND = "tailscale.policy"


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

    from hq.platform.application.inventory_testing import store

    from ..adoption import found_live

    spec = {"document": document or policy_document()}
    store(GATED_KIND, {"record": "policy", **spec})
    live = found_live(GATED_KIND, spec)
    assert live is not None
    save_adopted(live, key=POLICY_KEY, principal=cli_principal())
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
    user = operator(username=username)
    return web_principal(user)


class HeldRequestTests(TestCase):
    """A credential may ask. Asking is all it may do."""

    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        declare_policy()

    def test_declaring_a_second_one_is_held_as_firmly_as_amending_the_first(self):
        """A new declaration of a gated kind is new work for the controller."""

        result = execute_capability(
            "infrastructure.resource.create",
            keyed(
                {
                    "key": "another-policy",
                    "kind": GATED_KIND,
                    "spec": {"document": policy_document()},
                }
            ),
            principal=mcp_principal(),
        )

        self.assertEqual(result["status"], "awaiting_approval")
        self.assertFalse(ManagedResource.objects.filter(key="another-policy").exists())

    def test_a_caller_cannot_bury_the_queue_it_is_waiting_on(self):
        for index in range(MAX_PENDING_PER_ACTOR):
            answer = execute_capability(
                "infrastructure.resource.update",
                keyed(update_payload(policy_document(f"group:example{index}"))),
                principal=mcp_principal(),
                target=POLICY_KEY,
            )
            self.assertTrue(answer["ok"], answer)

        refused = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(policy_document("group:one-too-many"))),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )

        self.assertEqual(refused["error"]["code"], "too_many_pending_approvals")
        self.assertEqual(ApprovalRequest.objects.count(), MAX_PENDING_PER_ACTOR)


class DecisionTests(TestCase):
    """What a decision does, and what no decision can do."""

    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        declare_policy()

    def held(self, payload=None, capability="infrastructure.resource.reconcile"):
        answer = execute_capability(
            capability,
            payload or reconcile_payload(),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.assertTrue(answer["ok"], answer)
        return ApprovalRequest.objects.get()

    def test_an_operator_who_shares_the_requesting_name_is_refused(self):
        held = self.held()

        with self.assertRaises(AuthorizationError):
            approve(ApproveCommand(str(held.id)), principal=an_operator(held.requested_actor))

    def test_the_sweep_leaves_a_live_request_and_marks_one_nobody_answered_lapsed(self):
        from ..approvals import lapse_expired

        held = self.held()

        self.assertEqual(lapse_expired(), 0)
        self.assertEqual(pending(principal=cli_principal()), (held,))

        held.expires_at = timezone.now() - timezone.timedelta(minutes=1)
        held.save(update_fields=("expires_at",))

        self.assertEqual(lapse_expired(), 1)
        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.EXPIRED)


class SurfaceTests(TestCase):
    """A held request that nobody can see is a held request nobody answers."""

    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        declare_policy()
        self.user = operator(self.client, "reviewer")
        answer = execute_capability(
            "infrastructure.resource.update",
            keyed(update_payload(policy_document("group:elsewhere"))),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.assertTrue(answer["ok"], answer)
        self.held = ApprovalRequest.objects.get()

    def test_a_decision_that_cannot_be_taken_says_why_on_the_page(self):
        reject(RejectCommand(str(self.held.id)), principal=web_principal(self.user))

        response = self.client.post(
            reverse("control_plane:approval_decide", kwargs={"approval_id": self.held.id, "decision": "approve"}),
            form_keyed({}),
        )

        self.assertTrue(list(get_messages(response.wsgi_request)))
        self.held.refresh_from_db()
        self.assertEqual(self.held.state, ApprovalRequest.State.REJECTED)
