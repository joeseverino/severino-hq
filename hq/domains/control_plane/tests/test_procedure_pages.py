"""The procedures page: the adopted catalog, the newest scan and a proposal
a person reviews and adopts, each drawn from what the estate stored."""

import json
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import AdoptedCatalog, ApprovalRequest, OperationRequest, SourceProposal
from hq.platform.application.tests.markup import select, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _sent() -> dict[str, str]:
    """What a page's form posts with a press: the key a retry is known by."""

    from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

    return {RETRY_KEY_FIELD: new_key()}


def _words(html: str, section: str) -> str:
    return " ".join(element.words for element in within(html, "id", section))


def _links(html: str, section: str) -> set[str]:
    return {element.attrs["href"] for element in within(html, "id", section) if element.tag == "a"}


def _posts(html: str) -> set[str]:
    return {urlsplit(element.attrs["formaction"]).path for element in select(html, "formaction")}


class ProceduresPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        from hq.platform.application.automation_adoption import catch_up

        # Outside a request, this process reads the catalog seeded for the test.
        catch_up()
        self.client.force_login(self.owner)

    def page(self, url: str | None = None) -> str:
        response = self.client.get(url or reverse("control_plane:procedures"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def held(self) -> ApprovalRequest:
        from hq.platform.application.automation_adoption import ADOPT

        return ApprovalRequest.objects.get(capability=ADOPT, state=ApprovalRequest.State.PENDING)

    def proposal_url(self, pk: int) -> str:
        return reverse("control_plane:procedures_proposal", kwargs={"pk": pk})

    def test_each_adopted_procedure_links_to_its_command_page(self):
        from hq.platform.application.action_links import command_url
        from hq.platform.application.automation_adoption import procedures
        from hq.platform.application.automation_runs import capability_name

        adopted = procedures()
        self.assertTrue(adopted)
        links = _links(self.page(), "procedures-adopted")
        for procedure in adopted:
            with self.subTest(procedure=procedure.id):
                self.assertIn(command_url(capability_name(procedure.id)), links)

    def test_the_last_scan_says_how_it_ended(self):
        from hq.domains.jobs.models import JobState

        newest = OperationRequest.objects.filter(action=OperationRequest.Action.SCAN).order_by("-created_at").first()
        self.assertEqual(newest.state, OperationRequest.State.FAILED)
        said = _words(self.page(), "procedures-scan")
        self.assertIn(JobState.FAILED.label, said)
        self.assertIn(newest.result["message"], said)

    def test_a_commit_hq_cannot_adopt_lists_every_reason_and_offers_no_adopt(self):
        from hq.platform.application.automation_adoption import plan
        from hq.platform.application.record_errors import PolicyError

        newest = SourceProposal.objects.order_by("-pk").first()
        with self.assertRaises(PolicyError) as refusal:
            plan(newest.pk)
        html = self.page()
        said = _words(html, "procedures-proposal")
        self.assertIn(newest.revision[:12], said)
        for reason in refusal.exception.details["refused"]:
            self.assertIn(reason, said)
        self.assertNotIn(reverse("control_plane:procedures_adopt", kwargs={"pk": newest.pk}), _posts(html))

    def test_a_proposal_that_widens_says_each_widening_beside_its_changes_and_adopt(self):
        from hq.platform.application.automation_adoption import plan

        pk = self.held().payload["proposal"]
        planned = plan(pk)
        self.assertTrue(planned.widens)
        html = self.page(self.proposal_url(pk))
        said = _words(html, "procedures-proposal")
        for widening in planned.widens:
            self.assertIn(widening, said)
        # Each procedure the commit holds is a group of the change table.
        for named in planned.procedures:
            self.assertIn(named, said)
        self.assertIn(reverse("control_plane:procedures_adopt", kwargs={"pk": pk}), _posts(html))

    def test_an_adoption_waiting_for_a_person_links_its_decision_and_its_proposal(self):
        held = self.held()
        links = {element.attrs["href"] for element in select(self.page(), "href")}
        self.assertIn(reverse("core:approval_entry", kwargs={"approval_id": held.id}), links)
        self.assertIn(self.proposal_url(held.payload["proposal"]), links)

    def test_a_viewer_is_offered_neither_refresh_nor_adopt(self):
        from hq.platform.application.roles import Role, assign

        viewer = get_user_model().objects.create_user(username="example-viewer", password=None)
        assign(viewer, Role.VIEWER)
        self.client.force_login(viewer)
        pk = self.held().payload["proposal"]
        posts = _posts(self.page(self.proposal_url(pk)))
        self.assertNotIn(reverse("control_plane:procedures_refresh"), posts)
        self.assertNotIn(reverse("control_plane:procedures_adopt", kwargs={"pk": pk}), posts)

    def test_an_unknown_proposal_is_not_found(self):
        missing = (SourceProposal.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1
        self.assertEqual(self.client.get(self.proposal_url(missing)).status_code, 404)

    def test_adopt_makes_the_proposal_the_catalog(self):
        pk = self.held().payload["proposal"]
        response = self.client.post(reverse("control_plane:procedures_adopt", kwargs={"pk": pk}), _sent())
        self.assertRedirects(response, reverse("control_plane:procedures"), fetch_redirect_response=False)
        self.assertEqual(AdoptedCatalog.objects.first().proposal_id, pk)

    def test_adopting_a_commit_hq_cannot_adopt_changes_nothing(self):
        before = AdoptedCatalog.objects.first().digest
        newest = SourceProposal.objects.order_by("-pk").first()
        response = self.client.post(reverse("control_plane:procedures_adopt", kwargs={"pk": newest.pk}), _sent())
        self.assertRedirects(response, self.proposal_url(newest.pk), fetch_redirect_response=False)
        self.assertEqual(AdoptedCatalog.objects.first().digest, before)

    def test_refresh_queues_a_scan_its_page_follows(self):
        response = self.client.post(reverse("control_plane:procedures_refresh"), _sent(), **XHR)
        self.assertEqual(response.status_code, 202)
        answered = json.loads(response.content)
        self.assertEqual(answered["state"], "queued")
        followed = self.client.get(answered["status"])
        self.assertEqual(followed.status_code, 200)
        self.assertEqual(json.loads(followed.content)["state"], "queued")
        # A page loaded while it waits follows it too.
        (ask,) = select(self.page(), "data-ask-status")
        self.assertEqual(self.client.get(ask.attrs["data-ask-status"]).status_code, 200)

    def test_a_status_hq_did_not_sign_is_not_found(self):
        status = reverse("control_plane:procedures_status")
        for watch in ("", "forged", json.dumps({"scan": "x"})):
            with self.subTest(watch=watch):
                self.assertEqual(self.client.get(status, {"watch": watch}).status_code, 404)

    def test_a_scan_under_way_is_followed_and_a_commit_adopted_already_offers_nothing(self):
        from hq.platform.core.example_estate import automation

        automation.rescanned(self.owner)
        html = self.page()
        self.assertNotIn("data-ask-status", html)
        self.assertFalse({path for path in _posts(html) if path.endswith("/adopt/")})
        automation.scanning(self.owner)
        (ask,) = select(self.page(), "data-ask-status")
        self.assertEqual(json.loads(self.client.get(ask.attrs["data-ask-status"]).content)["state"], "running")


class OperationStandingTests(TestCase):
    """An operation a worker carries out stands as Jobs shows it."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def test_each_run_stands_in_the_state_jobs_shows_it_in(self):
        from django.utils import timezone

        from hq.domains.jobs.models import JobState
        from hq.platform.application.asks import DONE, FAILED, QUEUED, RUNNING, operation_standing
        from hq.platform.application.work_runs import LAPSED, run_seen_state

        expected = {JobState.QUEUED: QUEUED, JobState.RUNNING: RUNNING, JobState.SUCCEEDED: DONE}
        runs = OperationRequest.objects.annotate(seen=run_seen_state(timezone.now()))
        seen = set()
        for run in runs:
            standing = operation_standing(run)
            with self.subTest(run=str(run.pk), seen=run.seen):
                self.assertEqual(standing.state, expected.get(JobState(run.seen), FAILED))
                self.assertEqual(standing.label, JobState(run.seen).label)
                if run.seen == JobState.LOST and run.state == OperationRequest.State.CLAIMED:
                    self.assertEqual(standing.note, LAPSED)
            seen.add(run.seen)
        self.assertLessEqual({JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.LOST}, seen)
