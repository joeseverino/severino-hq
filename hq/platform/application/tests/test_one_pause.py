"""The Agents pause is one rule, read through one helper by every surface.

The MCP door, the machine API, approving what an agent asked for and the
controller's claim of what it queued each ask ``paused_interfaces``, the doors
through ``refusal`` with the principal they authenticated, and the other two
through ``held_askers``. Patched
to pause, every one refuses; patched to run while the stored switch says
paused, every one runs, so none reads the switch on its own. A person is
never paused.
"""

from unittest.mock import patch

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, OperationRequest
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.core.models import AgentAccess

from ..approvals import ApprovalError, ApproveCommand, approve
from ..capabilities import execute_capability
from ..controller import claim_next_operation
from ..security import Capability, Principal
from .test_approvals import GATED_KIND, POLICY_KEY, an_operator, declare_policy, reconcile_payload

HELPER = "hq.platform.application.agent_access.paused_interfaces"
PAUSED = frozenset({"mcp", "api"})
RUNNING = frozenset()
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


def _mcp_allowed() -> bool:
    """Whether the brake each MCP tool call asks lets the agent act."""

    from ..agent_admission import refused

    return refused(AGENT) is None


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class OnePauseTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        declare_policy()
        self.operator = an_operator()

    def _api_status(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")
        return response.status_code, response.json().get("error", {}).get("code")

    def _held(self, key):
        answer = execute_capability(
            "infrastructure.resource.reconcile", reconcile_payload(key), principal=AGENT, target=POLICY_KEY
        )
        self.assertTrue(answer["ok"], answer)
        return ApprovalRequest.objects.get(state=ApprovalRequest.State.PENDING)

    def _claim(self):
        return claim_next_operation("controller-example", capabilities=((GATED_KIND, "reconcile"),))["operation"]

    def test_the_helper_pausing_refuses_an_agent_on_every_surface(self):
        held = self._held("held")
        with patch(HELPER, return_value=PAUSED):
            self.assertFalse(_mcp_allowed())
            self.assertEqual(self._api_status(), (403, "agents_paused"))
            with self.assertRaises(ApprovalError):
                approve(ApproveCommand(str(held.id)), principal=self.operator)
        approve(ApproveCommand(str(held.id)), principal=self.operator)
        with patch(HELPER, return_value=PAUSED):
            self.assertIsNone(self._claim())
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.QUEUED)

    def test_no_surface_reads_the_switch_around_the_helper(self):
        """The stored switch says paused; the helper says run, and every surface runs."""

        held = self._held("around")
        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})
        with patch(HELPER, return_value=RUNNING):
            self.assertTrue(_mcp_allowed())
            self.assertEqual(self._api_status()[0], 200)
            approve(ApproveCommand(str(held.id)), principal=self.operator)
            self.assertIsNotNone(self._claim())

    def test_an_agent_s_queued_run_waits_out_the_pause(self):
        """What an agent asked a procedure to do is held from the automation worker too."""

        from .procedures import Supply, approved_run, manifest, run_payload

        Supply(self, manifest())
        answer = approved_run(self, run_payload(), principal=AGENT, key="example-run")
        self.assertTrue(answer["queued"], answer)

        def claim():
            return claim_next_operation("example-worker", capabilities=(("automation.operation", "run"),))["operation"]

        with patch(HELPER, return_value=PAUSED):
            self.assertIsNone(claim())
        self.assertEqual(claim()["id"], answer["operation"]["id"])

    def test_a_person_is_never_paused(self):
        """A person's own request runs and is claimed while agents are paused."""

        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})
        answer = execute_capability(
            "infrastructure.resource.reconcile", reconcile_payload("person"), principal=self.operator, target=POLICY_KEY
        )
        self.assertTrue(answer["ok"], answer)

        self.assertFalse(ApprovalRequest.objects.exists())
        self.assertIsNotNone(self._claim())


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class OneAdmissionTests(TestCase):
    """Both agent doors turn a bearer into a caller through one function and ask one brake,
    so a brake added inside ``agent_access.refusal`` changes neither door."""

    def test_both_doors_admit_a_bearer_through_one_function(self):
        from asgiref.sync import async_to_sync

        from hq.config import asgi

        from .. import agent_admission

        admitted = []
        admit = agent_admission.admit

        def spy(bearer, *, interface, source):
            answer = admit(bearer, interface=interface, source=source)
            admitted.append((interface, getattr(answer, "actor", None)))
            return answer

        token = _token(scope="read")
        with _serving(), patch.object(agent_admission, "admit", side_effect=spy):
            self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {token}")
            async_to_sync(asgi.admit_agent)(token, "100.64.0.10")

        self.assertEqual(admitted, [("api", "example-automation"), ("mcp", "example-automation")])

    def test_a_new_brake_reaches_the_machine_api_with_its_own_code(self):
        from ..agent_access import AgentRefusal

        blocked = AgentRefusal("example_blocked", "This agent is blocked.")
        with (
            _serving(),
            patch.dict("hq.platform.application.refusal_status.REFUSAL_STATUS", example_blocked=403),
            patch("hq.platform.application.agent_access.refusal", return_value=blocked),
        ):
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], {"code": "example_blocked", "message": "This agent is blocked."})
