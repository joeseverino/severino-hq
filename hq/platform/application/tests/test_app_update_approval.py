"""One approval of an app update previews its apply where the app runs, merges
its pull request at the head a person reviewed only once the preview passes,
and then applies the merge's commit through the adopted playbook that applies
app updates, as that person, once. Each step that ends badly says so on the
update, and stops what comes after it."""

from typing import Annotated

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from pydantic import Field

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, OperationRequest
from hq.platform.application.contracts import Closed
from hq.platform.core.testing import run_ended

from ..approvals import ApproveCommand, Settled, approve
from ..capabilities import run_capability
from ..controller import ControllerReport, claim_next_operation, report_operation
from ..machines import Machine
from ..resource_operations import serialize_operation
from ..security import Capability, PolicyDenied, Principal
from .procedures import APPROVER, Supply, manifest
from .test_app_updates import PINNED, UPDATER, arranged, moved, pull
from .test_github_estate import store

APPROVE = "app_update.approve"
# The machine the update's container runs on, which the playbook that applies it runs on.
APP = "example-box"
APPLY = "example.apps_apply"
APPLY_RUN = f"automation.{APPLY}.run"
CONTROLLER = "controller-example"
WORKER = "worker-example"
HEAD = "f" * 40
MERGED = "c" * 40
PERSON = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
AGENT = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
# The task a preview stops on when the host refuses the start, and what it says.
REFUSED_TASK = "Require sudo to let the deploy login start the pull unit"
REFUSED_SAID = "sudo does not let the deploy login start the pull unit."


class Revision(Closed):
    revision: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$", max_length=40)]


def applies(hosts=(APP,), **fields):
    return manifest(id=APPLY, input_type=Revision, hosts=hosts, fixes=("container-image-update",), **fields)


def stopped(task=REFUSED_TASK, message=REFUSED_SAID):
    return [
        {"host": APP, "task": "Ask sudo whether the deploy login may start it", "outcome": "ok", "message": ""},
        {"host": APP, "task": task, "outcome": "failed", "message": message},
    ]


class ApproveUpdateTests(TestCase):
    def setUp(self):
        from hq.platform.core.testing import managing_everything

        managing_everything()
        arranged(pull(moves=[moved()]))
        self.supply = Supply(self, applies())
        self.supply.machines = (*self.supply.machines, Machine(APP))

    def press(self, *, principal=PERSON, head=HEAD, key="example-approve-1"):
        payload = {"repository": "example/alpha", "number": 7, "head_sha": head}
        return run_capability(APPROVE, payload, principal=principal, idempotency_key=key).answer

    def merges(self):
        return OperationRequest.objects.filter(action=OperationRequest.Action.MERGE)

    def merge(self):
        return self.merges().get()

    def runs(self, **filters):
        return OperationRequest.objects.filter(action=OperationRequest.Action.RUN, **filters).order_by("created_at")

    def preview(self):
        return self.runs(input__check=True).get()

    def applies(self):
        return self.runs().exclude(input__has_key="check")

    def worked(self, *, success=True, events=(), message="", stated=None):
        """The worker's report of the run it claims next, and what its playbook ``stated``."""

        claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        run_ended(claimed["operation"]["id"], WORKER, events=events, success=success, message=message, stated=stated)
        return OperationRequest.objects.get(pk=claimed["operation"]["id"])

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

    def merged(self, **settled):
        """An approval whose preview passed and whose merge settled."""

        self.press()
        self.worked()
        return self.settle(**settled)

    def standing(self):
        from ..app_update_approval import merge_standing
        from ..app_updates import app_updates

        (update,) = app_updates()
        return merge_standing(update)

    def container_page(self, name="app"):
        from hq.platform.core.testing import declare, operator

        operator(self.client)
        spec = {"host": "example-box", "name": name, "connection_ref": "example-portainer"}
        declare(f"example-box-{name}", "portainer.container", spec)
        return self.client.get(reverse("control_plane:detail", args=[f"example-box-{name}"]))

    def test_a_persons_press_previews_the_apply_at_the_head_before_anything_merges(self):
        answer = self.press()

        self.assertTrue(answer["ok"], answer)
        self.assertFalse(self.merges().exists())
        preview = self.preview()
        self.assertEqual(preview.input["parameters"], {"revision": HEAD})
        self.assertEqual(preview.input["hosts"], [APP])
        self.assertEqual(preview.requested_actor, PERSON.actor)
        # Under way, said as its run stands, with its page.
        from ..work_runs import run_url

        self.assertTrue(self.standing().live)
        self.assertEqual(self.standing().run_url, run_url(str(preview.pk)))
        # The worker is told nothing of the steps after it.
        claimed = claim_next_operation(WORKER, capabilities=(("automation.operation", "run"),))
        self.assertNotIn("then", claimed["input"])
        # An API or MCP reader sees what the page shows.
        from ..container_reads import get_app_update

        record = get_app_update("example:alpha:7:app")
        self.assertEqual(record["merge"]["words"], self.standing().words)
        self.assertEqual(record["held"], self.standing().words)
        self.assertEqual(record["checks"]["state"], "success")

    def test_a_preview_that_fails_merges_nothing_and_the_update_says_where_it_stopped(self):
        from ..app_update_approval import approval_of
        from ..app_updates import app_updates
        from ..work_runs import run_url

        self.press()

        preview = self.worked(success=False, events=stopped())

        self.assertFalse(self.merges().exists())
        standing = self.standing()
        # It names the task it stopped on, in its own words as in its detail.
        self.assertEqual((standing.tone, standing.live), ("serious", False))
        self.assertIn(REFUSED_TASK, standing.words)
        self.assertIn(REFUSED_SAID, standing.lines)
        self.assertEqual(standing.run_url, run_url(str(preview.pk)))
        # Once the host is fixed it is approved again, which previews again.
        self.assertEqual(approval_of(app_updates()[0]).held, "")
        page = self.container_page()
        self.assertContains(page, REFUSED_TASK)
        self.assertContains(page, run_url(str(preview.pk)))
        self.assertTrue(self.press(key="example-approve-2")["ok"])
        self.assertEqual(self.runs(input__check=True).count(), 2)

    def test_a_retried_press_neither_previews_nor_merges_twice(self):
        from ..app_update_approval import succeeded

        self.assertTrue(self.press()["ok"])
        self.assertTrue(self.press()["ok"])
        # A second press of its own, while the first is under way, is answered
        # with where that one stands: never a refusal, and never a second run.
        again = self.press(key="example-approve-2")
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["standing"]["live"])
        self.assertEqual((again["queued"], again["operation"]["id"]), (False, str(self.runs().get().pk)))
        # Where a page follows the run it stands on.
        self.assertEqual(again["status_url"], reverse("jobs:run_status", args=[again["operation"]["id"]]))

        preview = self.worked()
        succeeded(preview)

        self.assertEqual(self.merges().count(), 1)
        self.assertEqual(self.runs().count(), 1)

    def test_a_preview_that_passes_merges_at_the_head_reviewed_as_the_person_who_approved(self):
        self.press()

        preview = self.worked()

        merge = self.merge()
        self.assertEqual(merge.resource.spec, {"repository": "example/alpha", "number": 7, "head_sha": HEAD})
        self.assertEqual(merge.input["then"]["operation"], APPLY)
        self.assertEqual(merge.input["then"]["hosts"], [APP])
        self.assertEqual(merge.requested_actor, PERSON.actor)
        self.assertEqual(merge.input["correlation"], preview.input["correlation"])
        self.assertEqual(preview.result["then"], {"merge": str(merge.pk)})
        self.assertEqual((self.standing().live, self.standing().operation), (True, merge))

    def test_the_container_page_draws_the_press_that_previews_the_update(self):
        page = self.container_page()
        approve = page.context["container"]["act"]

        self.assertFalse(approve.disabled, approve.title)
        # Beside it, what the press does.
        self.assertEqual(page.context["container"]["state"].said, f"Merges #7, then applies it on {APP}.")
        self.assertContains(page, "/approve/?next=")
        self.assertEqual(self.client.post(approve.url, {"__retry_key": "example-press"}).status_code, 302)
        self.assertEqual(self.preview().input["parameters"], {"revision": HEAD})

    def test_an_agents_call_waits_for_a_person(self):
        answer = self.press(principal=AGENT)

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(OperationRequest.objects.exists())
        # The person deciding sees each pin it moves, and every file it changes a link away.
        from ..approval_review import preview

        shown = preview(ApprovalRequest.objects.get())
        self.assertEqual([(row.label, row.change) for row in shown.rows], [("app app", "changed")])
        self.assertEqual(shown.more_url, "https://github.com/example/alpha/pull/7/files")

    def test_an_approved_agents_call_previews_and_merges_as_approved(self):
        from django.contrib.auth import get_user_model

        from ..security import web_principal

        self.press(principal=AGENT)
        approver, _ = get_user_model().objects.get_or_create(username=APPROVER)
        approve(ApproveCommand(str(ApprovalRequest.objects.get().pk)), principal=web_principal(approver))

        self.assertEqual(self.preview().input["approved_by"], APPROVER)
        self.worked()
        self.assertEqual(self.merge().input["approved_by"], APPROVER)

    def test_a_head_that_moves_while_it_waits_stales_the_approval(self):
        from django.contrib.auth import get_user_model

        from ..security import web_principal

        self.press(principal=AGENT)
        store(updater=UPDATER, pull_requests=[pull(moves=[moved()]) | {"head": "e" * 40}])

        approver, _ = get_user_model().objects.get_or_create(username=APPROVER)
        with self.assertRaises(Settled):
            approve(ApproveCommand(str(ApprovalRequest.objects.get().pk)), principal=web_principal(approver))
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_press_at_a_head_since_moved_is_refused(self):
        answer = self.press(head="e" * 40)

        self.assertFalse(answer["ok"], answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_only_renovates_own_change_to_the_apps_it_names_is_offered_approve(self):
        from ..app_update_approval import approval_of
        from ..app_updates import app_updates

        outside = {"path": ".github/workflows/ci.yml", "status": "modified", "additions": 1, "deletions": 1, "app": ""}
        renovates = pull(moves=[moved()])
        cases = (
            ("opened by another login", {"author": "example-writer"}),
            ("its head in a fork", {"head_repository": "example-fork/alpha"}),
            ("a file outside its apps", {"files": [*renovates["files"], outside], "changed_files": 2}),
            ("more files than HQ read", {"changed_files": 101}),
        )
        for n, (case, changed) in enumerate(cases):
            with self.subTest(case):
                store(compose=["compose/app/compose.yaml"], updater=UPDATER, pull_requests=[renovates | changed])

                answer = self.press(key=f"example-refused-{n}")

                self.assertFalse(answer["ok"], answer)
                # The card holds Approve back for the reason the press is refused.
                self.assertEqual(approval_of(app_updates()[0]).held, answer["error"]["message"])
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

    def test_an_update_where_the_playbook_never_runs_is_refused(self):
        from .procedures import APP as ELSEWHERE

        self.supply(applies(hosts=(ELSEWHERE,)))

        answer = self.press()

        self.assertFalse(answer["ok"], answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_playbook_written_for_more_machines_previews_and_applies_only_where_the_app_runs(self):
        from .procedures import APP as ELSEWHERE

        self.supply(applies(hosts=(APP, ELSEWHERE)))

        self.merged()

        self.assertEqual(self.preview().input["hosts"], [APP])
        self.assertEqual([run.input["hosts"] for run in self.applies()], [[APP]])

    def test_a_caller_kept_off_the_playbooks_run_may_not_approve_it(self):
        from django.contrib.auth import get_user_model

        from ..security import web_principal

        CapabilityRule.objects.create(subject="example-agent", capability=APPLY_RUN, rule="deny")
        self.press(principal=AGENT)

        approver, _ = get_user_model().objects.get_or_create(username=APPROVER)
        with self.assertRaises(PolicyDenied):
            approve(ApproveCommand(str(ApprovalRequest.objects.get().pk)), principal=web_principal(approver))
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_merge_queues_one_run_of_its_commit_as_the_person_who_approved(self):
        merge = self.merged()

        (run,) = self.applies()
        self.assertEqual(run.input["operation"], APPLY)
        self.assertEqual(run.input["parameters"], {"revision": MERGED})
        self.assertEqual(run.input["hosts"], [APP])
        self.assertEqual(run.requested_actor, PERSON.actor)
        # Who asked, as every page reads it: from the row that wrote it.
        self.assertEqual(serialize_operation(run)["requested_actor"], PERSON.actor)
        self.assertEqual(run.input["correlation"], merge.input["correlation"])
        self.assertEqual(merge.result["then"], {"run": str(run.pk)})
        # The update links the run, and the run's chain holds the merge of the pull request it applies.
        from hq.platform.core.testing import operator

        from ..work_runs import run_url

        self.assertEqual(self.standing().run_url, run_url(str(run.pk)))
        self.assertTrue(self.standing().live)
        operator(self.client)
        (steps,) = self.client.get(run_url(str(run.pk))).context["events"]
        merging = [step for step in steps.lines if step.page.operation_id == str(merge.pk)]
        self.assertEqual([step.url for step in merging], [merge.resource.get_absolute_url()])

    def merged_on_main(self):
        """Its pull request closed, and the default branch's head pinning what it merged."""

        image = {"path": "compose/app/compose.yaml", "service": "app", "image": PINNED}
        store(compose=["compose/app/compose.yaml"], head={"sha": MERGED}, compose_images=[image])

    def test_a_merge_whose_apply_fails_is_applied_at_main_in_one_press(self):
        self.merged()

        self.worked(success=False, events=stopped("Start the pull unit", "The pull unit failed."))

        standing = self.standing()
        self.assertEqual((standing.tone, standing.taken, standing.live), ("serious", True, False))
        self.assertIn("Start the pull unit", standing.words)
        self.assertIn("The pull unit failed.", standing.lines)
        self.merged_on_main()
        apply = self.container_page().context["container"]["act"]
        from .test_containers import named, queued

        # Its card keeps how it went, the run's page named by its standing.
        (card,) = [item for item in queued() if "app" in named(item, "Container")]
        run, *said = [value for label, value in card.evidence if label == "Apply"]
        self.assertEqual(run.url, reverse("jobs:run", kwargs={"pk": standing.run.id}))
        self.assertIn("Failed", run.label)
        self.assertIn("The pull unit failed.", said)
        self.assertEqual(self.client.post(apply.url, {"__retry_key": "example-apply"}).status_code, 302)
        again = self.applies().last()
        self.assertEqual((again.input["parameters"], again.input["hosts"]), ({"revision": MERGED}, [APP]))
        # Once asked, it is not offered again while it runs, and asks nothing of anyone.
        page = self.client.get(reverse("control_plane:detail", args=["example-box-app"]))
        self.assertIsNone(page.context["container"]["act"])
        self.assertEqual([item for item in queued("container-image-update") if "app" in named(item, "Container")], [])

    def test_a_second_apply_while_one_runs_is_answered_with_where_it_stands(self):
        from ..app_update_approval import APPLY as APPLY_UPDATE

        self.merged()
        self.worked(success=False, events=stopped("Start the pull unit", "The pull unit failed."))
        self.merged_on_main()
        payload = {"repository": "example/alpha", "app": "app", "head_sha": MERGED}

        first = run_capability(APPLY_UPDATE, payload, principal=PERSON, idempotency_key="example-apply-1").answer
        again = run_capability(APPLY_UPDATE, payload, principal=PERSON, idempotency_key="example-apply-2").answer

        self.assertTrue(first["ok"], first)
        self.assertTrue(again["ok"], again)
        self.assertTrue(again["standing"]["live"])
        self.assertEqual((again["queued"], again["operation"]["id"]), (False, first["operation"]["id"]))
        self.assertEqual(again["status_url"], reverse("jobs:run_status", args=[first["operation"]["id"]]))
        self.assertEqual(self.applies().count(), 2)

    def test_no_press_is_offered_while_another_run_holds_its_playbook_or_its_machine(self):
        from .procedures import RESTART_RUN, manifest

        self.supply(applies(), manifest(hosts=(APP,)))
        holders = {
            "playbook": (APPLY_RUN, {"hosts": [APP], "parameters": {"revision": "d" * 40}}),
            # Another playbook's run on the machine, before the one that applies has ever run.
            "machine": (RESTART_RUN, {"hosts": [APP], "parameters": {"service": "example"}}),
        }
        for holds, (capability, payload) in holders.items():
            with self.subTest(holds=holds), transaction.atomic():
                held = run_capability(capability, payload, principal=PERSON, idempotency_key=f"example-{holds}").answer
                self.assertTrue(held["ok"], held)

                page = self.container_page()
                # Said why, where the press would be, rather than refused once pressed.
                self.assertIsNone(page.context["container"]["act"])
                self.assertTrue(page.context["container"]["state"].reason)
                transaction.set_rollback(True)

    def built_after(self, *commits):
        """The container page of thumb, built on example-box and released
        there by the apply of the merge, where its repository's head is
        ``commits[0]`` and ``commits`` are those since the last that changed
        compose/thumb/."""

        from .test_containers import built

        self.merged()
        released = {"project": "thumb", "service": "thumb", "image": "example-thumb:local", "running_digest": ""}
        self.worked(stated={"readings": {APP: {"apps.pull": [{**released, "health": "healthy", "result": "ok"}]}}})
        built(head={"sha": commits[0]}, builds=[{"app": "thumb", "commits": list(commits)}])
        return self.container_page("thumb").context["container"]

    def test_an_app_built_on_its_host_changed_since_its_machine_released_it_is_applied_at_main(self):
        container = self.built_after("d" * 40, "e" * 40)

        self.assertEqual(container["state"].kind, "merged")
        self.assertEqual(self.client.post(container["act"].url, {"__retry_key": "example-apply"}).status_code, 302)
        again = self.applies().last()
        self.assertEqual((again.input["parameters"], again.input["hosts"]), ({"revision": "d" * 40}, [APP]))

    def test_an_app_built_on_its_host_released_at_or_after_its_last_change_is_current(self):
        container = self.built_after("d" * 40, MERGED, "e" * 40)

        self.assertEqual((container["state"], container["act"]), (None, None))

    def test_run_again_of_a_preview_fills_no_run_that_acts(self):
        from ..finding_runs import run_again

        self.press()
        preview = self.worked(success=False, events=stopped())

        self.assertEqual(run_again(APPLY_RUN, str(preview.pk)), {"hosts": [APP]})
        self.assertEqual(run_again("automation.example.other.run", str(preview.pk)), {})

    def test_a_merge_whose_apply_passes_stands_applied(self):
        self.merged()

        self.worked()

        self.assertEqual((self.standing().words, self.standing().tone), ("Applied", "good"))
        # A closed update that applied offers nothing more on the container's page.
        self.merged_on_main()
        container = self.container_page().context["container"]
        self.assertEqual((container["state"].standing.words, container["act"]), ("Applied", None))

    def test_an_apply_whose_machine_rolled_an_app_back_stands_applied_in_the_tone_to_look_at(self):
        self.merged()

        self.worked(stated={"readings": {APP: {"apps.pull": [{"project": "app", "result": "rolled_back"}]}}})

        standing = self.standing()
        self.assertEqual(standing.tone, "attention")
        self.assertTrue(any("app" in line and "Rolled back" in line for line in standing.lines), standing.lines)

    def test_a_container_read_running_an_older_pin_after_it_applied_is_applied_again(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderInventory

        self.merged()
        self.worked()
        self.merged_on_main()

        ProviderInventory.objects.filter(kind="portainer.container").update(observed_at=timezone.now())

        apply = self.container_page().context["container"]["act"]
        self.assertIsNotNone(apply)
        self.assertEqual(self.client.post(apply.url, {"__retry_key": "example-apply"}).status_code, 302)
        self.assertEqual(self.applies().count(), 2)

    def test_a_replayed_settle_queues_no_second_run(self):
        from ..app_update_approval import succeeded

        merge = self.merged()

        succeeded(merge)

        self.assertEqual(self.applies().count(), 1)

    def test_a_refused_merge_queues_no_run(self):
        self.merged(success=False, status={"refused": "head_moved"})

        self.assertFalse(self.applies().exists())
        # Said on the update, and pressed again once its head is reviewed.
        self.assertEqual(self.standing().words, "GitHub refused the merge: the head moved after the approval.")
        self.assertFalse(self.standing().taken)

    def test_a_merge_github_refused_is_merged_by_the_next_approval_at_that_head(self):
        self.merged(success=False, status={"refused": "not_mergeable"})

        self.assertTrue(self.press(key="example-approve-2")["ok"])
        self.worked()

        self.assertEqual(self.merges().filter(state=OperationRequest.State.QUEUED).count(), 1)
        self.assertEqual((self.standing().live, self.standing().taken), (True, True))

    def test_a_merge_commit_that_is_no_sha_queues_no_run(self):
        merge = self.merged(status={"merge_sha": "not-a-sha"})

        self.assertFalse(self.applies().exists())
        self.assertIn("refused", merge.result["then"])
        self.assertEqual(self.standing().tone, "serious")

    def test_a_new_adoption_since_the_approval_runs_nothing(self):
        from .procedures import NEXT_REVISION

        self.press()
        self.worked()
        self.supply(applies(revision=NEXT_REVISION))

        merge = self.settle()

        self.assertFalse(self.applies().exists())
        self.assertIn("refused", merge.result["then"])
