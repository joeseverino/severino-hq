"""The Playbooks page: the adopted catalog, the commit it was adopted at on
the title's line, and, while a newer scanned commit waits, a person's View
diff, Adopt all and Reject all; and the scan's own page, which View diff
opens, each drawn from what the estate stored."""

import json
from urllib.parse import urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import AdoptedCatalog, ApprovalRequest, OperationRequest, SourceProposal
from hq.platform.application.tests.markup import elements, select, text, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

XHR = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def _sent() -> dict[str, str]:
    """What a page's form posts with a press: the key a retry is known by."""

    from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

    return {RETRY_KEY_FIELD: new_key()}


def _says_each_widening_once(case, elements, planned) -> None:
    """Each way ``planned`` widens what a named playbook may do is said by
    that playbook's rows among ``elements``, marked for attention, and never
    again in a sentence of its own."""

    said = " ".join(element.words for element in elements)
    marked = [element for element in elements if element.tag == "tr" and "data-attention" in element.attrs]
    titles = {procedure.title for procedure in planned.procedures.values()}
    named = {title: widening for widening in planned.widens for title in titles if widening.startswith(f"{title} ")}
    case.assertTrue(named, "the estate's commit widens what a playbook may do")
    for title, widening in named.items():
        with case.subTest(playbook=title):
            case.assertTrue(any(title in row.words for row in marked), "its row is marked")
            case.assertNotIn(widening.removeprefix(title).strip().rstrip("."), said)


def _posts(html: str) -> set[str]:
    return {urlsplit(element.attrs["formaction"]).path for element in select(html, "formaction")}


def _head(html: str) -> list:
    return within(html, "class", "page-head")


def _adopt(pk: int) -> str:
    return reverse("control_plane:playbooks_adopt", kwargs={"pk": pk})


def _reject(pk: int) -> str:
    return reverse("control_plane:playbooks_reject", kwargs={"pk": pk})


def _scan_page(proposal: SourceProposal) -> str:
    return reverse("jobs:run", kwargs={"pk": proposal.operation_id})


class PlaybooksPageTests(TestCase):
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
        response = self.client.get(url or reverse("control_plane:playbooks"))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def newest(self) -> SourceProposal:
        return SourceProposal.objects.order_by("-pk").first()

    def test_each_adopted_procedure_links_to_its_command_page(self):
        from hq.platform.application.action_links import command_url
        from hq.platform.application.automation_adoption import procedures
        from hq.platform.application.automation_runs import capability_name

        adopted = procedures()
        self.assertTrue(adopted)
        links = {
            element.attrs["href"] for element in within(self.page(), "data-adopted-playbooks") if element.tag == "a"
        }
        for procedure in adopted:
            with self.subTest(procedure=procedure.id):
                self.assertIn(command_url(capability_name(procedure.id)), links)

    def test_the_adopted_table_says_where_each_playbook_runs_and_its_access_as_its_run_page_does(self):
        from hq.platform.application.automation_adoption import procedures
        from hq.platform.application.automation_runs import access_words

        said = " ".join(element.words for element in within(self.page(), "data-adopted-playbooks"))
        for procedure in procedures():
            with self.subTest(procedure=procedure.id):
                self.assertNotIn(procedure.id, said, "the title links its page, so its id is not said again")
                if procedure.privilege:
                    self.assertIn(access_words(procedure.privilege), said)
                if not procedure.hosts:
                    self.assertIn("Any machine", said)
        self.assertIn("No sudo", said)
        self.assertNotIn("–", said)

    def test_the_page_is_the_table_alone_under_no_heading_of_its_own(self):
        html = self.page()
        self.assertFalse(select(html, "class", "page-lede"))
        headings = [heading for heading in elements(html, "h2") if not heading.attrs.get("id", "").startswith("modal-")]
        self.assertFalse(headings, "no section heads the table, and no scan result has a box")
        self.assertFalse(select(html, "data-approval"), "no request waits on this page")

    def test_the_title_line_names_the_commit_adopted_in_the_place_of_when_the_repository_was_read(self):
        adopted = AdoptedCatalog.objects.order_by("-pk").first()
        html = self.page()
        (line,) = select(html, "data-adopted-commit")
        for said in (adopted.revision[:12], adopted.branch, f"from {adopted.committer}", "adopted"):
            self.assertIn(said, line.words)
        self.assertFalse(select(html, "data-reading-age"))

    def test_the_title_says_in_one_line_why_the_newest_scan_failed_and_when_one_last_reached_it(self):
        newest = OperationRequest.objects.filter(action=OperationRequest.Action.SCAN).order_by("-created_at").first()
        self.assertEqual(newest.state, OperationRequest.State.FAILED)
        html = self.page()
        (failed,) = select(html, "data-scan-failed")
        self.assertIn(newest.result["message"], failed.words)
        self.assertIn("Last reached", failed.words)
        # A Refresh still waiting leaves the failure said.
        self.client.post(reverse("control_plane:playbooks_refresh"), _sent(), **XHR)
        (still,) = select(self.page(), "data-scan-failed")
        self.assertIn(newest.result["message"], still.words)
        # A scan that read the repository leaves only the commit adopted.
        from hq.platform.core.example_estate import automation

        OperationRequest.objects.filter(action=OperationRequest.Action.SCAN, state="queued").delete()
        automation.rescanned(self.owner)
        html = self.page()
        self.assertFalse(select(html, "data-scan-failed"))
        self.assertEqual(len(select(html, "data-adopted-commit")), 1)

    def test_a_newer_commit_puts_view_diff_adopt_all_and_reject_all_beside_refresh(self):
        newest = self.newest()
        html = self.page()
        head = _head(html)
        self.assertIn(_scan_page(newest), {element.attrs.get("href") for element in head})
        self.assertLessEqual({_adopt(newest.pk), _reject(newest.pk)}, _posts(html))
        (actions,) = [element for element in head if "page-actions" in element.classes]
        self.assertIn("data-many", actions.attrs, "every one stands beside the title")
        labels = [element.words for element in head if element.tag in {"a", "button"} and element.words]
        self.assertLess(labels.index("Refresh"), labels.index("View diff"))
        self.assertLess(labels.index("View diff"), labels.index("Adopt all"))
        self.assertLess(labels.index("Adopt all"), labels.index("Reject all"))

    def test_view_diff_opens_the_scan_s_page_with_who_refreshed_both_commits_and_each_playbook_s_rows(self):
        from hq.platform.application.automation_adoption import plan

        newest = self.newest()
        planned = plan(newest.pk)
        html = self.page(_scan_page(newest))
        said = text(html)
        adopted = AdoptedCatalog.objects.order_by("-pk").first()
        for fact in ("Asked by", "Commit read", newest.revision[:12], "Commit adopted", adopted.revision[:12]):
            self.assertIn(fact, said)
        self.assertIn(newest.operation.requested_actor, said)
        table = within(html, "class", "data-table")
        _says_each_widening_once(self, table, planned)
        rows = " ".join(element.words for element in table)
        # Each playbook is named by its title, a row of the change table or
        # its foot's unchanged, never by its id.
        for named, procedure in planned.procedures.items():
            self.assertIn(procedure.title, said)
            self.assertNotIn(named, rows)
        self.assertLessEqual({_adopt(newest.pk), _reject(newest.pk)}, _posts(html))

    def test_a_commit_hq_cannot_adopt_offers_reject_all_and_says_each_problem_on_its_playbook_s_row(self):
        from hq.platform.application.automation_adoption import judged

        # The commit before the newest is the one HQ can't adopt.
        self.newest().delete()
        newest = self.newest()
        planned = judged(newest.pk)
        self.assertTrue(planned.unadoptable)
        html = self.page()
        self.assertIn(_reject(newest.pk), _posts(html))
        self.assertNotIn(_adopt(newest.pk), _posts(html))
        scan = self.page(_scan_page(newest))
        self.assertNotIn(_adopt(newest.pk), _posts(scan))
        rows = [element for element in within(scan, "class", "data-table") if element.tag == "tbody"]
        problems = {
            shown["_summary"]["label"]: shown["problems"]
            for shown in planned.compared["after"]["procedures"].values()
            if "problems" in shown
        }
        self.assertEqual(set(problems), set(planned.unadoptable))
        for named, said in problems.items():
            (group,) = [row for row in rows if named in row.words]
            with self.subTest(playbook=named):
                self.assertTrue(said)
                for problem in said:
                    self.assertIn(problem, group.words)

    def test_reject_all_leaves_nothing_waiting_and_the_scan_s_page_says_who_rejected_it(self):
        newest = self.newest()
        response = self.client.post(_reject(newest.pk), _sent())
        self.assertRedirects(response, reverse("control_plane:playbooks"), fetch_redirect_response=False)
        newest.refresh_from_db()
        self.assertEqual(newest.rejected_by, self.owner.get_username())
        html = self.page()
        self.assertNotIn(_scan_page(newest), {element.attrs.get("href") for element in _head(html)})
        self.assertFalse({path for path in _posts(html) if path.endswith(("/adopt/", "/reject/"))})
        scan = self.page(_scan_page(newest))
        self.assertIn("Rejected by", text(scan))
        self.assertFalse({path for path in _posts(scan) if path.endswith(("/adopt/", "/reject/"))})

    def test_adopt_all_makes_the_proposal_the_catalog(self):
        newest = self.newest()
        response = self.client.post(_adopt(newest.pk), _sent())
        self.assertRedirects(response, reverse("control_plane:playbooks"), fetch_redirect_response=False)
        self.assertEqual(AdoptedCatalog.objects.first().proposal_id, newest.pk)
        self.assertFalse({path for path in _posts(self.page()) if path.endswith(("/adopt/", "/reject/"))})

    def test_adopting_a_commit_hq_cannot_adopt_changes_nothing_and_goes_back_to_its_scan(self):
        before = AdoptedCatalog.objects.first().digest
        self.newest().delete()
        refused = self.newest()
        response = self.client.post(_adopt(refused.pk), _sent())
        self.assertRedirects(response, _scan_page(refused), fetch_redirect_response=False)
        self.assertEqual(AdoptedCatalog.objects.first().digest, before)

    def test_no_held_adoption_waits_for_anyone(self):
        from hq.platform.application.automation_adoption import ADOPT

        self.assertFalse(ApprovalRequest.objects.filter(capability=ADOPT).exists())

    def test_the_source_s_record_lives_on_this_page_and_offers_neither_edit_nor_removal(self):
        from hq.domains.control_plane.models import ManagedResource
        from hq.platform.application.automation_sources import SOURCE_KEY
        from hq.platform.application.resource_capabilities import resource_capabilities

        playbooks = reverse("control_plane:playbooks")
        self.assertRedirects(self.client.get(reverse("control_plane:detail", kwargs={"key": SOURCE_KEY})), playbooks)
        self.assertEqual(resource_capabilities(ManagedResource.objects.get(key=SOURCE_KEY)).offered, ("", ""))
        changes = {reverse(f"control_plane:{route}", kwargs={"key": SOURCE_KEY}) for route in ("edit", "remove")}
        links = {urlsplit(element.attrs["href"]).path for element in select(self.page(), "href")}
        self.assertFalse(changes & links)

    def test_a_viewer_is_offered_neither_refresh_adopt_nor_reject(self):
        from hq.platform.application.roles import Role, assign

        newest = self.newest()
        viewer = get_user_model().objects.create_user(username="example-viewer", password=None)
        assign(viewer, Role.VIEWER)
        self.client.force_login(viewer)
        posts = _posts(self.page())
        self.assertNotIn(reverse("control_plane:playbooks_refresh"), posts)
        self.assertFalse({_adopt(newest.pk), _reject(newest.pk)} & posts)

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
        answered = json.loads(self.client.post(reverse("control_plane:playbooks_refresh"), _sent(), **XHR).content)
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
        answered = json.loads(self.client.post(reverse("control_plane:playbooks_refresh"), _sent(), **XHR).content)
        self.assertIn("playbook worker", answered["note"])
        # What else the worker is doing, and when it took it, is not the reader's question.
        self.assertNotIn("took", answered["note"])
        # A worker busy elsewhere is coming, however long the scan has waited.
        with mock.patch("hq.platform.application.automation_sources.CLAIM_EXPECTED", timedelta(0)):
            late = json.loads(self.client.get(answered["status"]).content)
        self.assertNotIn("hq-host automation", late["note"])

    def test_refresh_queues_a_scan_its_page_follows(self):
        response = self.client.post(reverse("control_plane:playbooks_refresh"), _sent(), **XHR)
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
        status = reverse("control_plane:playbooks_status")
        for watch in ("", "forged", json.dumps({"scan": "x"})):
            with self.subTest(watch=watch):
                self.assertEqual(self.client.get(status, {"watch": watch}).status_code, 404)

    def test_a_scan_of_the_commit_adopted_offers_only_refresh_and_a_scan_under_way_is_followed(self):
        from hq.platform.core.example_estate import automation

        automation.rescanned(self.owner)
        html = self.page()
        self.assertNotIn("data-ask-status", html)
        self.assertFalse({path for path in _posts(html) if path.endswith(("/adopt/", "/reject/"))})
        self.assertNotIn("View diff", text(html))
        automation.scanning(self.owner)
        (ask,) = select(self.page(), "data-ask-status")
        self.assertEqual(json.loads(self.client.get(ask.attrs["data-ask-status"]).content)["state"], "running")


class EmptyPlaybooksPageTests(TestCase):
    """Before any scan: what to press, and nothing about a read that never happened."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("empty") as owner:
            cls.owner = owner

    def test_with_no_scan_the_title_says_so(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("control_plane:playbooks")).content.decode()
        (none,) = select(html, "data-scan-none")
        self.assertIn("No scan yet", none.words)
        self.assertFalse(select(html, "data-adopted-commit"))
        self.assertFalse(select(html, "data-scan-failed"))
        self.assertIn("No playbooks are adopted yet.", text(html))


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
