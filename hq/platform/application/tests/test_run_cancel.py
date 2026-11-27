"""A playbook's run nothing has claimed can be cancelled, so it holds its
playbook and machines no more: by a person with the run's own authority,
from where the run is listed or from the refusal it caused, and by an agent
for a run it asked for, over the same command."""

from django.test import TestCase

from hq.domains.control_plane.models import OperationRequest

from ..capabilities import run_capability
from ..controller import claim_next_operation
from ..security import Capability, Principal
from .procedures import APP, RESTART_RUN, Supply, manifest, run_payload

CANCEL = "automation.run.cancel"
OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
LOGS = "example.service_logs"
WORKER = "example-worker"


class CancelTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest(), manifest(id=LOGS))
        self.keys = iter(range(1000))

    def ask(self, *hosts, principal=OPERATOR, name=RESTART_RUN):
        answer = run_capability(
            name, run_payload(*hosts), principal=principal, idempotency_key=f"example-{next(self.keys)}"
        ).answer
        self.assertTrue(answer["ok"], answer)
        return answer

    def cancel(self, operation_id, *, principal=OPERATOR):
        return run_capability(
            CANCEL,
            {"operation_id": str(operation_id)},
            principal=principal,
            idempotency_key=f"cancel-{next(self.keys)}",
        ).answer

    def refused(self, operation_id, code, **asked):
        answer = self.cancel(operation_id, **asked)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["code"], code, answer)
        return answer["error"]

    def state(self, operation_id):
        return OperationRequest.objects.get(pk=operation_id).state


class PersonCancelsTests(CancelTestCase):
    def test_a_claimed_run_is_left_to_its_runner(self):
        run = self.ask(APP)["operation"]
        claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))

        error = self.refused(run["id"], "precondition")

        self.assertEqual(error["details"]["operation"]["id"], run["id"])
        self.assertEqual(self.state(run["id"]), OperationRequest.State.CLAIMED)

    def test_an_ended_run_is_refused(self):
        run = self.ask(APP)["operation"]
        self.cancel(run["id"])

        self.refused(run["id"], "precondition")

    def test_an_id_no_run_has_is_not_found(self):
        for unknown in ("00000000-0000-0000-0000-000000000000", "not-an-id"):
            with self.subTest(unknown):
                self.refused(unknown, "not_found")
