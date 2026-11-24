"""One approval of an app update merges its pull request at the head a person
reviewed, and the merge's commit is then applied by the adopted playbook that
applies app updates, as that person, once."""

from typing import Annotated

from django.test import TestCase
from pydantic import Field

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, OperationRequest
from hq.platform.application.contracts import Closed

from ..approvals import ApproveCommand, Settled, approve
from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..resource_operations import serialize_operation
from ..security import Capability, PolicyDenied, Principal
from .procedures import APP, APPROVER, Supply, manifest
from .test_app_updates import arranged, patch, pull
from .test_github_estate import store

APPROVE = "app_update.approve"
APPLY = "example.apps_apply"
CONTROLLER = "controller-example"
HEAD = "f" * 40
MERGED = "c" * 40
PERSON = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))


class Revision(Closed):
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", max_length=40)]


def applies(**fields):
    return manifest(id=APPLY, input_type=Revision, hosts=(APP,), fixes=("container-image-update",), **fields)


class ApproveUpdateTests(TestCase):
    def setUp(self):
        from hq.platform.application.adoption_testing import managing_everything

        managing_everything()
        arranged(pull(diff=patch()))
        self.supply = Supply(self, applies())

    def press(self, *, principal=PERSON, head=HEAD, key="example-approve-1"):
        payload = {"repository": "example/alpha", "number": 7, "head_sha": head}
        return run_capability(APPROVE, payload, principal=principal, idempotency_key=key).answer

    def merge(self):
        return OperationRequest.objects.get(action=OperationRequest.Action.MERGE)

    def settle(self, *, success=True, status=None):
        claimed = claim_next_operation(CONTROLLER, capabilities=(("github.pull_request", "merge"),))["operation"]
        report_operation(
            claimed["id"],
            ControllerReport(
                success=success,
                observed_generation=self.merge().input["generation"],
                status={"merge_sha": MERGED} if status is None else status,
            ),
            controller_id=CONTROLLER,
        )
        return self.merge()

    def runs(self):
        return OperationRequest.objects.filter(action=OperationRequest.Action.RUN)

    def test_a_persons_press_queues_the_merge_at_the_head_reviewed(self):
        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        merge = self.merge()
        self.assertEqual(merge.resource.spec, {"repository": "example/alpha", "number": 7, "head_sha": HEAD})
        self.assertEqual(merge.input["then"]["operation"], APPLY)
        self.assertEqual(merge.input["then"]["hosts"], [APP])
        self.assertFalse(self.runs().exists())

    def test_an_agents_call_waits_for_a_person(self):
        answer = self.press(principal=AGENT)

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_head_that_moves_while_it_waits_stales_the_approval(self):
        from django.contrib.auth import get_user_model

        from ..security import web_principal

        self.press(principal=AGENT)
        store(pull_requests=[pull(diff=patch()) | {"head": "e" * 40}])

        approver, _ = get_user_model().objects.get_or_create(username=APPROVER)
        with self.assertRaises(Settled):
            approve(ApproveCommand(str(ApprovalRequest.objects.get().pk)), principal=web_principal(approver))
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_press_at_a_head_since_moved_is_refused(self):
        answer = self.press(head="e" * 40)

        self.assertFalse(answer["ok"], answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_no_playbook_adopted_to_apply_it_is_refused_with_the_next_step(self):
        self.supply()

        answer = self.press()

        self.assertFalse(answer["ok"], answer)
        self.assertIn("Playbooks", answer["error"]["message"])
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_caller_kept_off_the_playbooks_run_may_not_approve_it(self):
        from django.contrib.auth import get_user_model

        from ..security import web_principal

        CapabilityRule.objects.create(subject="example-agent", capability=f"automation.{APPLY}.run", rule="deny")
        self.press(principal=AGENT)

        approver, _ = get_user_model().objects.get_or_create(username=APPROVER)
        with self.assertRaises(PolicyDenied):
            approve(ApproveCommand(str(ApprovalRequest.objects.get().pk)), principal=web_principal(approver))
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_merge_queues_one_run_of_its_commit_as_the_person_who_approved(self):
        self.press()

        merge = self.settle()

        (run,) = self.runs()
        self.assertEqual(run.input["operation"], APPLY)
        self.assertEqual(run.input["parameters"], {"revision": MERGED})
        self.assertEqual(run.input["hosts"], [APP])
        self.assertEqual(run.requested_actor, PERSON.actor)
        # Who asked, as every page reads it: from the row that wrote it.
        self.assertEqual(serialize_operation(run)["requested_actor"], PERSON.actor)
        self.assertEqual(run.input["correlation"], merge.input["correlation"])
        self.assertEqual(merge.result["then"], {"run": str(run.pk)})

    def test_a_replayed_settle_queues_no_second_run(self):
        from ..app_update_approval import merged

        self.press()
        merge = self.settle()

        merged(merge)

        self.assertEqual(self.runs().count(), 1)

    def test_a_refused_merge_queues_no_run(self):
        self.press()

        self.settle(success=False, status={"refused": "head_moved"})

        self.assertFalse(self.runs().exists())

    def test_a_merge_commit_that_is_no_sha_queues_no_run(self):
        self.press()

        merge = self.settle(status={"merge_sha": "not-a-sha"})

        self.assertFalse(self.runs().exists())
        self.assertIn("refused", merge.result["then"])

    def test_a_new_adoption_since_the_approval_runs_nothing(self):
        from .procedures import NEXT_REVISION

        self.press()
        self.supply(applies(revision=NEXT_REVISION))

        merge = self.settle()

        self.assertFalse(self.runs().exists())
        self.assertIn("refused", merge.result["then"])
