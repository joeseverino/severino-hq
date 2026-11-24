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
from .test_app_updates import arranged, moved, pull
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
        arranged(pull(moves=[moved()]))
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

    def standing(self):
        from ..app_update_approval import merge_standing
        from ..app_updates import app_updates

        (update,) = app_updates()
        return merge_standing(update)

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
        self.assertEqual((self.standing().words, self.standing().taken), ("Merging", True))
        # An API or MCP reader sees what the page shows.
        from ..container_reads import get_app_update

        record = get_app_update("example:alpha:7:app")
        self.assertEqual((record["merge"]["words"], record["held"]), ("Merging", "Merging"))
        self.assertEqual(record["checks"]["state"], "success")

    def test_the_container_page_draws_the_press_that_queues_the_merge(self):
        from django.urls import reverse

        from hq.platform.core.testing import declare, operator

        operator(self.client)
        spec = {"host": "example-box", "name": "app", "connection_ref": "example-portainer"}
        declare("example-box-app", "portainer.container", spec)

        page = self.client.get(reverse("control_plane:detail", args=["example-box-app"]))
        approve = page.context["container"]["approve"]

        self.assertFalse(approve.disabled, approve.title)
        # Beside it, what the press does.
        self.assertEqual(page.context["container"]["approval"].said, f"Merges #7, then applies it on {APP}.")
        self.assertContains(page, "/approve/?next=")
        self.assertEqual(self.client.post(approve.url, {"__retry_key": "example-press"}).status_code, 302)
        self.assertEqual(self.merge().resource.spec["head_sha"], HEAD)

    def test_the_update_card_offers_one_press_approve(self):
        from .test_containers import queued

        arranged(pull(moves=[moved()]), advisories=())

        (card,) = [item for item in queued() if ("Container", "app") in item.evidence]

        self.assertEqual([step.link.capability for step in card.steps], [APPROVE])
        self.assertEqual(card.no_help_reason, "")

    def test_an_agents_call_waits_for_a_person(self):
        answer = self.press(principal=AGENT)

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(OperationRequest.objects.exists())
        # The person deciding sees each pin it moves, and every file it changes a link away.
        from ..approval_review import preview

        shown = preview(ApprovalRequest.objects.get())
        self.assertEqual([(row.label, row.change) for row in shown.rows], [("app app", "changed")])
        self.assertEqual(shown.more_url, "https://github.com/example/alpha/pull/7/files")

    def test_a_head_that_moves_while_it_waits_stales_the_approval(self):
        from django.contrib.auth import get_user_model

        from ..security import web_principal

        self.press(principal=AGENT)
        store(pull_requests=[pull(moves=[moved()]) | {"head": "e" * 40}])

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
        from ..app_update_approval import approval_of
        from ..app_updates import app_updates

        # The page and the finding hold the press back for the same reason.
        self.assertEqual(approval_of(app_updates()[0]).held, answer["error"]["message"])
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
        # The update links the run, and the run names the pull request it applies.
        from ..run_pages import run_context
        from ..work_runs import run_url

        self.assertEqual(self.standing().run_url, run_url(str(run.pk)))
        context = run_context(
            {"operation_id": run.pk, "approved_by": "", "capability": "", "playbook": "", "state_label": ""}, user=None
        )
        self.assertEqual(context["merged_from"]["label"], "example/alpha#7")

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
        # Said on the update, and pressed again once its head is reviewed.
        self.assertEqual(self.standing().words, "GitHub refused the merge: the head moved after the approval.")
        self.assertFalse(self.standing().taken)

    def test_a_merge_commit_that_is_no_sha_queues_no_run(self):
        self.press()

        merge = self.settle(status={"merge_sha": "not-a-sha"})

        self.assertFalse(self.runs().exists())
        self.assertIn("refused", merge.result["then"])
        self.assertEqual(self.standing().tone, "serious")

    def test_a_new_adoption_since_the_approval_runs_nothing(self):
        from .procedures import NEXT_REVISION

        self.press()
        self.supply(applies(revision=NEXT_REVISION))

        merge = self.settle()

        self.assertFalse(self.runs().exists())
        self.assertIn("refused", merge.result["then"])
