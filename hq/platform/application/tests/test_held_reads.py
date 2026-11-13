"""A held request is read by its asker alone, by the id its held answer
carried, under that caller's read grant: where it stands, the work an
approved call queued, and what the call answered once that work ended."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from hq.domains.control_plane.models import OperationRequest

from ..approvals import ApproveCommand, RejectCommand, approve, reject
from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..record_errors import RecordNotFound
from ..resources import get_resource
from ..security import AuthorizationError, Capability, Principal, web_principal
from .procedures import RESTART_RUN, Supply, manifest, run_payload

GRANT = frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE})
AGENT = Principal("example-agent", "mcp", GRANT)
# Another agent, and the same agent's name reached through another door.
OTHER = Principal("example-other", "mcp", GRANT)
SAME_NAME_ELSEWHERE = Principal("example-agent", "api", GRANT)
UNREAD = Principal("example-agent", "mcp", frozenset({Capability.MANAGE_INFRASTRUCTURE}))
HELD = "approvals"
WORKER = "example-worker"


@override_settings(SEVERINO_SITE_HOST="hq.example.com")
class HeldReadTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())
        answer = run_capability(RESTART_RUN, run_payload(), principal=AGENT, idempotency_key="example-run").answer
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.held_id = answer["approval"]["id"]
        self.person = web_principal(get_user_model().objects.create_user(username="example-owner"))

    def read(self, principal=AGENT):
        return get_resource(HELD, self.held_id, principal=principal)

    def test_a_waiting_request_reads_as_waiting_with_nothing_queued(self):
        held = self.read()

        self.assertEqual((held["id"], held["state"], held["capability"]), (self.held_id, "pending", RESTART_RUN))
        self.assertEqual((held["done"], held["operation"], held["answer"]), (False, "", {}))
        self.assertIn(held["review_url"], held["said"])
        self.assertTrue(held["asks"].startswith("example-agent wants to"))

    def test_an_approved_run_names_its_operation_then_answers_once_it_ends(self):
        approve(ApproveCommand(self.held_id), principal=self.person)

        applying = self.read()
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        operation = OperationRequest.objects.get()
        report_operation(
            str(operation.pk),
            ControllerReport(success=True, observed_generation=operation.input["generation"], message="Restarted."),
            controller_id=WORKER,
        )
        ended = self.read()

        self.assertEqual((applying["state"], applying["done"], applying["answer"]), ("approved", False, {}))
        self.assertEqual(applying["operation"], str(operation.pk))
        self.assertTrue(ended["done"])
        self.assertEqual(ended["answer"]["operation"]["id"], str(operation.pk))
        self.assertEqual(ended["answer"]["operation"]["state"], OperationRequest.State.SUCCEEDED)

    def test_a_declined_request_answers_the_refusal_its_call_would(self):
        reject(RejectCommand(self.held_id, note="Not this week."), principal=self.person)

        held = self.read()

        self.assertTrue(held["done"])
        self.assertEqual(held["answer"]["error"]["code"], "declined")
        self.assertFalse(OperationRequest.objects.exists())

    def test_another_caller_s_request_reads_as_none_at_all(self):
        missing = "00000000-0000-4000-8000-000000000000"
        for principal, identifier in (
            (OTHER, self.held_id),
            (SAME_NAME_ELSEWHERE, self.held_id),
            (self.person, self.held_id),
            (AGENT, missing),
            (AGENT, "not-an-id"),
        ):
            with self.subTest(actor=principal.actor, interface=principal.interface, identifier=identifier):
                with self.assertRaises(RecordNotFound) as refused:
                    get_resource(HELD, identifier, principal=principal)
                self.assertNotIn(self.held_id, f"{refused.exception} {refused.exception.details}")

    def test_a_caller_without_the_read_grant_is_refused(self):
        with self.assertRaises(AuthorizationError):
            self.read(UNREAD)
