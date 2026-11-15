"""A declaration is written only under the runner's consent, whatever writes it.

A writer takes the runner's consent rather than a principal, and only the
runner makes one, so the hold cannot be walked around by calling a writer;
and the consent judges the kind actually written, so a command that never
says it acts on a declaration cannot write a gated one past the hold.
"""

import copy
import json
import pickle
from dataclasses import replace

from django.contrib.auth import get_user_model
from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource, OperationRequest

from ..approvals import ApproveCommand, approve
from ..capabilities import execute_capability
from ..infrastructure import ManagedResourceCommand, save_managed_resource
from ..resource_operations import OperationCommand, request_reconcile
from ..security import Capability, Principal, mcp_principal, web_principal
from .writers import keyed

TOKEN = Principal("a-token", "mcp", frozenset({Capability.MANAGE_INFRASTRUCTURE}))
POLICY_KEY = "access-policy"
# A kind that waits for a person, from anybody without one.
GATED_KIND = "tailscale.policy"


def policy_document(who: str = "group:example") -> str:
    return json.dumps(
        {
            "tagOwners": {"tag:example": ["autogroup:admin"]},
            "grants": [{"src": [who], "dst": ["tag:example"], "ip": ["*"]}],
        },
        indent=2,
    )


def an_operator() -> Principal:
    return web_principal(get_user_model().objects.create_user(username="operator", password="test-only-password"))


def amended() -> ManagedResourceCommand:
    return ManagedResourceCommand(key=POLICY_KEY, kind=GATED_KIND, spec={"document": policy_document("group:else")})


class ConsentTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        ManagedResource.objects.create(key=POLICY_KEY, kind=GATED_KIND, spec={"document": policy_document()})

    def policy(self) -> str:
        return ManagedResource.objects.get(key=POLICY_KEY).spec["document"]

    def test_a_writer_cannot_be_called_with_a_principal_alone(self):
        with self.assertRaises(TypeError):
            save_managed_resource(amended(), principal=TOKEN, current_key=POLICY_KEY)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            request_reconcile(OperationCommand(reason=""), principal=TOKEN, current_key=POLICY_KEY)  # type: ignore[call-arg]

        self.assertEqual(self.policy(), policy_document())
        self.assertFalse(OperationRequest.objects.exists())

    def test_only_the_runner_makes_a_consent(self):
        from hq.platform.application.tests.consent import consented

        from ..capabilities import Consented

        with self.assertRaises(TypeError):
            Consented(TOKEN)  # type: ignore[call-arg]
        with self.assertRaises(TypeError):
            Consented(TOKEN, approved=True, issuer=object())
        given = consented(TOKEN)
        # Not rebound to another principal, nor marked approved, once made.
        with self.assertRaises(TypeError):
            replace(given, principal=TOKEN)  # type: ignore[type-var]
        with self.assertRaises(AttributeError):
            given.approved = True  # type: ignore[misc]

    def test_a_command_about_something_else_writing_a_gated_kind_as_an_agent_is_refused(self):
        from hq.platform.application.tests.consent import another_command_writing

        from ..capabilities import NEEDS_APPROVAL

        def write(consented):
            save_managed_resource(amended(), consented=consented, current_key=POLICY_KEY)

        with another_command_writing(write) as name:
            answer = execute_capability(name, {"idempotency_key": "tidy-once"}, principal=mcp_principal())

        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["message"], NEEDS_APPROVAL)
        self.assertEqual(self.policy(), policy_document())

    def test_the_same_command_run_by_a_person_writes(self):
        from hq.platform.application.tests.consent import another_command_writing

        def write(consented):
            save_managed_resource(amended(), consented=consented, current_key=POLICY_KEY)

        with another_command_writing(write) as name:
            answer = execute_capability(name, {"idempotency_key": "tidy-once"}, principal=an_operator())

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(self.policy(), amended().spec["document"])

    def test_a_held_change_a_person_approves_is_written(self):
        wanted = policy_document("group:approved")
        asked = execute_capability(
            "infrastructure.resource.update",
            keyed({"key": POLICY_KEY, "kind": GATED_KIND, "spec": {"document": wanted}, "enabled": True}),
            principal=mcp_principal(),
            target=POLICY_KEY,
        )
        self.assertEqual(asked["status"], "awaiting_approval", asked)
        self.assertEqual(self.policy(), policy_document())

        approve(ApproveCommand(asked["approval"]["id"]), principal=an_operator())

        self.assertEqual(self.policy(), wanted)


class AdoptedTests(TestCase):
    """Writing down what is live asks no consent, so only a read of the last sweep reaches it."""

    def test_a_spec_a_caller_wrote_is_not_written_down_as_adopted(self):
        from ..adoption import Live
        from ..infrastructure import save_adopted

        with self.assertRaises(TypeError):
            Live(GATED_KIND, {"document": policy_document("group:else")}, reader=object())
        with self.assertRaises(TypeError):
            save_adopted(amended(), principal=TOKEN)  # type: ignore[call-arg]

        self.assertFalse(ManagedResource.objects.exists())

    def test_what_the_last_sweep_found_is_written_down(self):
        from hq.platform.application.inventory_testing import store

        from ..adoption import found_live
        from ..infrastructure import save_adopted

        spec = {"document": policy_document("group:live")}
        store(GATED_KIND, {"record": "policy", **spec})
        live = found_live(GATED_KIND, spec)
        assert live is not None

        save_adopted(live, key=POLICY_KEY, principal=TOKEN)

        self.assertEqual(ManagedResource.objects.get(key=POLICY_KEY).spec["document"], spec["document"])

    def test_what_a_caller_does_to_the_spec_it_is_handed_is_not_written_down(self):
        from hq.platform.application.inventory_testing import store

        from ..adoption import found_live
        from ..infrastructure import save_adopted

        spec = {"document": policy_document("group:live")}
        store(GATED_KIND, {"record": "policy", **spec})
        live = found_live(GATED_KIND, spec)
        assert live is not None

        live.spec["document"] = policy_document("group:else")
        save_adopted(live, key=POLICY_KEY, principal=TOKEN)

        self.assertEqual(ManagedResource.objects.get(key=POLICY_KEY).spec["document"], spec["document"])


class TokenTests(TestCase):
    """A consent and a read of the sweep are made only by their modules: no copy,
    pickle or bare instance gives a caller one it can use."""

    def made_again(self):
        return {
            "copy": copy.copy,
            "deepcopy": copy.deepcopy,
            "pickle": lambda token: pickle.loads(pickle.dumps(token)),
        }

    def test_a_consent_is_not_copied_pickled_or_made_bare(self):
        from hq.platform.application.tests.consent import consented

        from ..capabilities import Consented

        given = consented(TOKEN)
        for way, again in self.made_again().items():
            with self.subTest(way=way), self.assertRaises((TypeError, AttributeError)):
                again(given)
        with self.assertRaises(AttributeError):
            object.__new__(Consented).require(GATED_KIND)

    def test_a_read_of_the_sweep_is_not_copied_pickled_or_made_bare(self):
        from hq.platform.application.inventory_testing import store

        from ..adoption import Live, found_live
        from ..infrastructure import save_adopted

        spec = {"document": policy_document("group:live")}
        store(GATED_KIND, {"record": "policy", **spec})
        live = found_live(GATED_KIND, spec)
        assert live is not None
        for way, again in self.made_again().items():
            with self.subTest(way=way), self.assertRaises((TypeError, AttributeError)):
                again(live)
        with self.assertRaises(AttributeError):
            save_adopted(object.__new__(Live), key=POLICY_KEY, principal=TOKEN)

        self.assertFalse(ManagedResource.objects.exists())


class SweepAdoptionTests(TestCase):
    """The controller's report is evidence of what exists, never consent to act on
    it: a kind that waits for a person is taken on switched off, until one switches it on."""

    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()

    def swept(self):
        from ..security import cli_principal
        from ..sweep import record_sweep

        record_sweep(
            {
                GATED_KIND: {"ok": True, "records": [{"record": "policy", "document": policy_document("group:live")}]},
                "tailscale.device": {"ok": True, "records": [{"name": "a-box", "key_expires": "", "tags": []}]},
            },
            principal=cli_principal(),
        )

    def test_a_kind_that_waits_for_a_person_is_adopted_switched_off(self):
        self.swept()

        self.assertEqual(
            dict(ManagedResource.objects.values_list("kind", "enabled")), {GATED_KIND: False, "tailscale.device": True}
        )

    def test_a_person_switching_it_on_is_the_consent_and_an_agent_waits(self):
        self.swept()
        key = ManagedResource.objects.get(kind=GATED_KIND).key
        switched_on = keyed({"enabled": True})

        asked = execute_capability("infrastructure.resource.update", switched_on, principal=mcp_principal(), target=key)
        self.assertEqual(asked["status"], "awaiting_approval", asked)
        self.assertFalse(ManagedResource.objects.get(key=key).enabled)

        done = execute_capability(
            "infrastructure.resource.update", keyed({"enabled": True}), principal=an_operator(), target=key
        )
        self.assertTrue(done["ok"], done)
        self.assertTrue(ManagedResource.objects.get(key=key).enabled)

    def test_keeping_what_is_live_over_a_gated_kind_takes_a_persons_consent(self):
        from hq.platform.application.tests.consent import another_command_writing

        from ..capabilities import NEEDS_APPROVAL
        from ..resource_operations import OperationCommand, accept_observed

        self.swept()
        key = ManagedResource.objects.get(kind=GATED_KIND).key

        def write(consented):
            accept_observed(OperationCommand(reason=""), consented=consented, current_key=key)

        with another_command_writing(write) as name:
            answer = execute_capability(name, {"idempotency_key": "keep-once"}, principal=mcp_principal())

        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["message"], NEEDS_APPROVAL)
