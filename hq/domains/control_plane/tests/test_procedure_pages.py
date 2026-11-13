"""The procedures page: the adopted catalog, when the repository was last
read, and the one commit to decide on, each drawn from what the estate
stored; a held adoption as the approval card its audit entry draws."""

import json
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import AdoptedCatalog, ApprovalRequest, OperationRequest, SourceProposal
from hq.platform.application.tests.markup import select, text, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _sent() -> dict[str, str]:
    """What a page's form posts with a press: the key a retry is known by."""

    from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

    return {RETRY_KEY_FIELD: new_key()}


def _words(html: str, section: str) -> str:
    return " ".join(element.words for element in within(html, "id", section))


def _card(html: str) -> str:
    """The words of the approval card a held request is drawn by."""

    return " ".join(element.words for element in within(html, "data-approval", "pending"))


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

    def test_the_adopted_table_says_where_each_playbook_runs_and_its_access_as_its_run_page_does(self):
        from hq.platform.application.automation_adoption import procedures

        said = _words(self.page(), "procedures-adopted")
        for procedure in procedures():
            with self.subTest(procedure=procedure.id):
                self.assertNotIn(procedure.id, said, "the title links its page, so its id is not said again")
                if procedure.privilege:
                    self.assertIn(f"sudo, as the {procedure.privilege} profile allows", said)
                if not procedure.hosts:
                    self.assertIn("Any machine", said)
        self.assertIn("No sudo", said)
        self.assertNotIn("–", said)

    def test_no_playbooks_page_carries_a_lede(self):
        pk = self.held().payload["proposal"]
        refused = SourceProposal.objects.order_by("-pk").first().pk
        for html in (self.page(), self.page(self.proposal_url(pk)), self.page(self.proposal_url(refused))):
            with self.subTest():
                self.assertFalse(select(html, "class", "page-lede"))

    def test_a_commit_s_own_page_draws_only_that_commit(self):
        held = self.held().payload["proposal"]
        self.assertTrue(within(self.page(), "id", "procedures-adopted"))
        self.assertFalse(within(self.page(self.proposal_url(held)), "id", "procedures-adopted"))
        adopted = AdoptedCatalog.objects.order_by("-pk").first().proposal
        self.assertTrue(within(self.page(self.proposal_url(adopted.pk)), "id", "procedures-adopted"))

    def test_a_refused_commit_says_what_is_wrong_and_nothing_more(self):
        refused = SourceProposal.objects.order_by("-pk").first()
        for html in (self.page(), self.page(self.proposal_url(refused.pk))):
            with self.subTest():
                told = within(html, "class", "notice-error")
                self.assertTrue([element for element in told if element.tag == "li"])
                self.assertFalse([element for element in told if element.tag == "p"])

    def test_each_commit_says_who_committed_it(self):
        adopted = AdoptedCatalog.objects.order_by("-pk").first()
        self.assertIn(f"committed by {adopted.committer}", _words(self.page(), "procedures-adopted"))

    def decided(self) -> int:
        """The held adoption's proposal, its request declined, so the page asks about it itself."""

        held = self.held()
        decision = reverse("control_plane:approval_decision", kwargs={"approval_id": held.id})
        self.client.post(decision, {"decision": "reject", **_sent()})
        held.refresh_from_db()
        self.assertEqual(held.state, ApprovalRequest.State.REJECTED)
        return held.payload["proposal"]

    def test_the_title_says_in_one_line_why_the_newest_scan_failed_and_when_one_last_reached_it(self):
        newest = OperationRequest.objects.filter(action=OperationRequest.Action.SCAN).order_by("-created_at").first()
        self.assertEqual(newest.state, OperationRequest.State.FAILED)
        html = self.page()
        (failed,) = select(html, "data-scan-failed")
        self.assertIn(newest.result["message"], failed.words)
        self.assertIn("Last reached", failed.words)
        # One line on the repository's reach: no age of its own beside the failure.
        self.assertFalse(select(html, "data-reading-age"))
        # A Refresh still waiting leaves the failure said.
        self.client.post(reverse("control_plane:procedures_refresh"), _sent(), **XHR)
        (still,) = select(self.page(), "data-scan-failed")
        self.assertIn(newest.result["message"], still.words)
        # A scan that read the repository leaves only when it did.
        from hq.platform.core.example_estate import automation

        OperationRequest.objects.filter(action=OperationRequest.Action.SCAN, state="queued").delete()
        automation.rescanned(self.owner)
        html = self.page()
        self.assertFalse(select(html, "data-scan-failed"))
        self.assertEqual(len(select(html, "data-reading-age", "read")), 1)

    def test_the_page_reads_from_what_runs_now_to_the_newest_commit_to_what_waits(self):
        html = self.page()
        order = [html.index(mark) for mark in ('id="procedures-adopted"', 'id="procedures-proposal"', "data-approval=")]
        self.assertEqual(order, sorted(order))
        newest = SourceProposal.objects.order_by("-pk").first()
        said = _words(html, "procedures-proposal")
        self.assertIn(newest.revision[:12], said)
        self.assertIn("The newest commit HQ has read", said)
        self.assertIn("HQ can't adopt this commit", said)
        # The held commit is the card's, the adopted one the table's.
        self.assertIn(str(self.held().baseline["revision"])[:12], _card(html))
        adopted = AdoptedCatalog.objects.order_by("-pk").first()
        self.assertIn(adopted.revision[:12], _words(html, "procedures-adopted"))

    def test_a_card_on_the_page_links_nowhere_for_the_record_this_page_is(self):
        here = reverse("control_plane:procedures")
        targets = {
            urlsplit(element.attrs["href"]).path
            for element in within(self.page(), "data-approval")
            if element.tag == "a" and "href" in element.attrs
        }
        self.assertNotIn(here, targets)

    def stop_the_worker(self):
        """No run holds the worker: each claim's lease has run out."""

        from datetime import timedelta

        from django.utils import timezone

        OperationRequest.objects.filter(state=OperationRequest.State.CLAIMED).update(
            lease_expires_at=timezone.now() - timedelta(minutes=1)
        )

    def test_a_refresh_says_what_it_waits_on_and_what_to_do_when_it_does_not_come(self):
        from datetime import timedelta
        from unittest import mock

        self.stop_the_worker()
        answered = json.loads(self.client.post(reverse("control_plane:procedures_refresh"), _sent(), **XHR).content)
        self.assertEqual(answered["state"], "queued")
        self.assertIn("playbook worker", answered["note"])
        self.assertNotIn("hq-host automation", answered["note"])
        # Past when a worker comes, it says how to start one, and where none can be.
        with mock.patch("hq.platform.application.automation_sources.CLAIM_EXPECTED", timedelta(0)):
            late = json.loads(self.client.get(answered["status"]).content)
            # A page loaded while it waits says the same.
            waiting = " ".join(element.words for element in within(self.page(), "data-ask"))
        self.assertEqual(late["state"], "queued")
        self.assertIn("hq-host automation", late["note"])
        self.assertIn("development server has no worker", late["note"])
        self.assertIn("hq-host automation", waiting)

    def test_a_refresh_behind_work_the_worker_carries_waits_for_it_in_a_few_words(self):
        from datetime import timedelta
        from unittest import mock

        self.assertTrue(OperationRequest.objects.filter(state=OperationRequest.State.CLAIMED).exists())
        answered = json.loads(self.client.post(reverse("control_plane:procedures_refresh"), _sent(), **XHR).content)
        self.assertIn("playbook worker", answered["note"])
        # What else the worker is doing, and when it took it, is not the reader's question.
        self.assertNotIn("took", answered["note"])
        # A worker busy elsewhere is coming, however long the scan has waited.
        with mock.patch("hq.platform.application.automation_sources.CLAIM_EXPECTED", timedelta(0)):
            late = json.loads(self.client.get(answered["status"]).content)
        self.assertNotIn("hq-host automation", late["note"])

    def test_a_commit_hq_cannot_adopt_lists_every_reason_and_offers_no_adopt(self):
        from hq.platform.application.automation_adoption import plan
        from hq.platform.application.record_errors import PolicyError

        newest = SourceProposal.objects.order_by("-pk").first()
        with self.assertRaises(PolicyError) as refusal:
            plan(newest.pk)
        html = self.page()
        said = text(html)
        self.assertIn(newest.revision[:12], said)
        for reason in refusal.exception.details["refused"]:
            self.assertIn(reason, said)
        self.assertNotIn(reverse("control_plane:procedures_adopt", kwargs={"pk": newest.pk}), _posts(html))

    def test_a_held_adoption_is_the_approval_card_its_audit_entry_draws(self):
        from hq.platform.application.automation_adoption import plan

        held = self.held()
        decision = reverse("control_plane:approval_decision", kwargs={"approval_id": held.id})
        entry = self.client.get(reverse("core:approval_entry", kwargs={"approval_id": held.id}), follow=True)
        widens = plan(held.payload["proposal"]).widens
        self.assertTrue(widens)
        for html in (self.page(), self.page(self.proposal_url(held.payload["proposal"])), entry.content.decode()):
            card = _card(html)
            self.assertNotIn(held.resource_key, card, "the card names its record, never by its key")
            for widening in widens:
                self.assertIn(widening, card)
            self.assertIn(decision, _posts(html))
            self.assertNotIn(
                reverse("control_plane:procedures_adopt", kwargs={"pk": held.payload["proposal"]}), _posts(html)
            )
        # The card is the one way to it: no link sends the reader to decide it elsewhere.
        hrefs = {element.attrs["href"] for element in select(self.page(), "href")}
        self.assertNotIn(reverse("core:approval_entry", kwargs={"approval_id": held.id}), hrefs)
        self.assertNotIn(self.proposal_url(held.payload["proposal"]), hrefs)

    def test_a_held_adoption_says_each_widening_once_beside_the_playbook_it_names(self):
        from hq.platform.application.automation_adoption import plan

        held = self.held()
        widens = plan(held.payload["proposal"]).widens
        self.assertTrue(widens)
        html = self.page()
        card = within(html, "data-approval", "pending")
        heads = [element for element in card if "row-group" in element.attrs.get("class", "").split()]
        for widening in widens:
            with self.subTest(widening=widening):
                (head,) = [head for head in heads if widening in head.words]
                self.assertIn("data-attention", head.attrs)
        self.assertFalse(
            [element for element in card if "notice" in element.attrs.get("class", "").split()],
            "a widening a playbook carries is not said again above the table",
        )

    def test_a_commit_no_request_holds_says_each_widening_beside_its_changes_and_adopt(self):
        from hq.platform.application.automation_adoption import plan

        pk = self.decided()
        planned = plan(pk)
        self.assertTrue(planned.widens)
        html = self.page(self.proposal_url(pk))
        self.assertFalse(_card(html))
        said = _words(html, "procedures-proposal")
        for widening in planned.widens:
            self.assertIn(widening, said)
        # Each playbook the commit holds is a group of the change table, by
        # the title the Adopted table reads it by, never by its id.
        for named, procedure in planned.procedures.items():
            self.assertIn(procedure.title, said)
            self.assertNotIn(named, said)
        self.assertIn(reverse("control_plane:procedures_adopt", kwargs={"pk": pk}), _posts(html))

    def test_the_source_s_record_lives_on_this_page_and_offers_neither_edit_nor_removal(self):
        from hq.domains.control_plane.models import ManagedResource
        from hq.platform.application.automation_sources import SOURCE_KEY
        from hq.platform.application.resource_capabilities import resource_capabilities

        procedures = reverse("control_plane:procedures")
        self.assertRedirects(self.client.get(reverse("control_plane:detail", kwargs={"key": SOURCE_KEY})), procedures)
        self.assertEqual(resource_capabilities(ManagedResource.objects.get(key=SOURCE_KEY)).offered, ("", ""))
        changes = {reverse(f"control_plane:{route}", kwargs={"key": SOURCE_KEY}) for route in ("edit", "remove")}
        decision = reverse("core:approval_entry", kwargs={"approval_id": self.held().id})
        for url in (procedures, decision):
            with self.subTest(url=url):
                page = self.client.get(url, follow=True)
                self.assertEqual(page.status_code, 200)
                links = {urlsplit(element.attrs["href"]).path for element in select(page.content.decode(), "href")}
                self.assertFalse(changes & links)

    def test_a_viewer_is_offered_neither_refresh_nor_adopt(self):
        from hq.platform.application.roles import Role, assign

        pk = self.decided()
        viewer = get_user_model().objects.create_user(username="example-viewer", password=None)
        assign(viewer, Role.VIEWER)
        self.client.force_login(viewer)
        posts = _posts(self.page(self.proposal_url(pk)))
        self.assertNotIn(reverse("control_plane:procedures_refresh"), posts)
        self.assertNotIn(reverse("control_plane:procedures_adopt", kwargs={"pk": pk}), posts)

    def test_an_unknown_proposal_is_not_found(self):
        missing = (SourceProposal.objects.order_by("-pk").values_list("pk", flat=True).first() or 0) + 1
        self.assertEqual(self.client.get(self.proposal_url(missing)).status_code, 404)

    def test_adopt_makes_the_proposal_the_catalog(self):
        pk = self.decided()
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


class EmptyProceduresPageTests(TestCase):
    """Before any scan: what to press, and nothing about a read that never happened."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("empty") as owner:
            cls.owner = owner

    def test_with_no_scan_the_title_says_to_press_refresh(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("control_plane:procedures")).content.decode()
        (none,) = select(html, "data-scan-none")
        self.assertIn("No scan yet", none.words)
        self.assertFalse(select(html, "data-reading-age"))
        self.assertFalse(select(html, "data-scan-failed"))
        self.assertFalse(within(html, "id", "procedures-proposal"))


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
