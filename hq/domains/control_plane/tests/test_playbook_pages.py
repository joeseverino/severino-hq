"""The Playbooks page: while a newer scanned commit waits, each playbook's change
with its own Adopt and Reject, and Adopt all and Reject all, each press writing
what it says; and where a worker is missing, how to set one up."""

from urllib.parse import urlsplit

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from hq.domains.control_plane.models import AdoptedCatalog, ApprovalRequest, OperationRequest, SourceProposal
from hq.platform.application.markup_testing import select, with_class
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator


def _sent() -> dict[str, str]:
    """What a page's form posts with a press: the key a retry is known by."""

    from hq.platform.application.web_writes import RETRY_KEY_FIELD, new_key

    return {RETRY_KEY_FIELD: new_key()}


def _posts(html: str) -> set[str]:
    return {urlsplit(element.attrs["formaction"]).path for element in select(html, "formaction")}


def _adopt(pk: int) -> str:
    return reverse("control_plane:playbooks_adopt", kwargs={"pk": pk})


def _reject(pk: int) -> str:
    return reverse("control_plane:playbooks_reject", kwargs={"pk": pk})


def _scan_page(proposal: SourceProposal) -> str:
    from hq.platform.application.automation_sources import entry_url

    return entry_url(proposal)


def _decisions(html: str) -> dict[str, dict[str, str]]:
    """Each one-playbook decision a page offers: by the playbook it names,
    each button's words and where it posts, with the kind of change its row says."""

    offered: dict[str, dict[str, str]] = {}
    for sent in select(html, "name", "playbook"):
        # The button that sends it, through the page's shared form.
        rows = [element for element in sent.ancestors if "data-change" in element.attrs]
        found = offered.setdefault(sent.attrs["value"], {"change": rows[0].attrs["data-change"] if rows else ""})
        found[sent.words] = urlsplit(sent.attrs["formaction"]).path
    return offered


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

    def test_the_page_reads_the_adopted_catalog_once_within_its_query_budget(self):
        self.page()
        with CaptureQueriesContext(connection) as queries:
            self.page()

        # The catalog's digest alone is how the request catches up (``served``).
        whole = [query for query in queries if f'"{AdoptedCatalog._meta.db_table}"."procedures"' in query["sql"]]
        self.assertEqual(len(whole), 1, "every part of the page names the one catalog it read")
        self.assertLessEqual(len(queries), 18)

    def test_while_a_commit_waits_each_change_offers_its_adopt_and_reject(self):
        from hq.platform.application.automation_adoption import judged

        newest = self.newest()
        changes = {change.key: change.change for change in judged(newest.pk).changes}
        self.assertEqual(set(changes.values()), {"added", "changed", "removed"}, "the estate's commit holds each kind")
        html = self.page()
        offered = _decisions(html)
        self.assertEqual(set(offered), set(changes))
        for key, change in changes.items():
            with self.subTest(playbook=key):
                self.assertEqual(offered[key]["change"], change)
                self.assertEqual(offered[key]["Adopt"], _adopt(newest.pk))
                self.assertEqual(offered[key]["Reject"], _reject(newest.pk))

    def test_adopting_one_playbook_runs_only_it_from_the_new_commit_and_the_rest_still_wait(self):
        from hq.platform.application.automation_adoption import judged, procedures
        from hq.platform.core.example_estate import automation

        newest = self.newest()
        before = {procedure.id: procedure.revision for procedure in procedures()}
        response = self.client.post(_adopt(newest.pk), {**_sent(), "playbook": automation.CACHE_FLUSH})
        self.assertRedirects(response, reverse("control_plane:playbooks"), fetch_redirect_response=False)
        from hq.platform.application.automation_adoption import catch_up

        catch_up()
        after = {procedure.id: procedure.revision for procedure in procedures()}
        self.assertEqual(after[automation.CACHE_FLUSH], automation.WIDENED_REVISION)
        self.assertEqual(
            {key: value for key, value in after.items() if key != automation.CACHE_FLUSH},
            {key: value for key, value in before.items() if key != automation.CACHE_FLUSH},
        )
        waiting = {change.key for change in judged(newest.pk).changes}
        self.assertNotIn(automation.CACHE_FLUSH, waiting)
        self.assertTrue(waiting, "the other changes still wait")
        self.assertEqual(set(_decisions(self.page())), waiting)

    def test_rejecting_one_playbook_keeps_it_as_adopted_and_waiting_no_more(self):
        from hq.platform.application.automation_adoption import judged, procedures
        from hq.platform.core.example_estate import automation

        newest = self.newest()
        response = self.client.post(_reject(newest.pk), {**_sent(), "playbook": automation.PACKAGE_UPGRADE})
        self.assertRedirects(response, reverse("control_plane:playbooks"), fetch_redirect_response=False)
        self.assertIn(automation.PACKAGE_UPGRADE, {procedure.id for procedure in procedures()})
        self.assertNotIn(automation.PACKAGE_UPGRADE, {change.key for change in judged(newest.pk).changes})
        self.assertNotIn(automation.PACKAGE_UPGRADE, _decisions(self.page()))
        # Adopt all now takes what still waits, and the rejected playbook stays.
        self.client.post(_adopt(newest.pk), _sent())
        from hq.platform.application.automation_adoption import catch_up

        catch_up()
        self.assertIn(automation.PACKAGE_UPGRADE, {procedure.id for procedure in procedures()})
        self.assertFalse(_decisions(self.page()))

    def test_view_diff_opens_the_audit_entry_of_the_read_where_each_change_is_decided(self):
        from hq.platform.application.automation_adoption import plan

        newest = self.newest()
        planned = plan(newest.pk)
        url = _scan_page(newest)
        self.assertTrue(url.startswith(reverse("core:audit_list")), "View diff opens an audit entry")
        html = self.page(url)
        # Each change has its own Adopt and Reject there too, and a press comes back to it.
        offered = _decisions(html)
        self.assertEqual(set(offered), {change.key for change in planned.changes})
        key = next(iter(offered))
        response = self.client.post(_reject(newest.pk), {**_sent(), "playbook": key, "next": url})
        self.assertRedirects(response, url, fetch_redirect_response=False)

    def test_a_commit_hq_cannot_adopt_offers_only_reject(self):
        from hq.platform.application.automation_adoption import judged

        # The commit before the newest is the one HQ can't adopt.
        self.newest().delete()
        newest = self.newest()
        planned = judged(newest.pk)
        refused = {change.title: change for change in planned.changes if change.problems}
        self.assertEqual(set(refused), set(planned.unadoptable))
        self.assertEqual({change.change for change in refused.values()}, {"added", "changed"}, "it holds each kind")
        html = self.page()
        self.assertIn(_reject(newest.pk), _posts(html))
        self.assertNotIn(_adopt(newest.pk), _posts(html))
        self.assertFalse([key for key, offered in _decisions(html).items() if "Adopt" in offered])
        scan = self.page(_scan_page(newest))
        self.assertNotIn(_adopt(newest.pk), _posts(scan))

    def test_reject_all_leaves_nothing_waiting(self):
        newest = self.newest()
        response = self.client.post(_reject(newest.pk), _sent())
        self.assertRedirects(response, reverse("control_plane:playbooks"), fetch_redirect_response=False)
        html = self.page()
        self.assertFalse({path for path in _posts(html) if path.endswith(("/adopt/", "/reject/"))})
        scan = self.page(_scan_page(newest))
        self.assertFalse({path for path in _posts(scan) if path.endswith(("/adopt/", "/reject/"))})

    def test_a_commit_hq_can_adopt_all_but_one_of_offers_the_rest_in_one_press_and_keeps_that_one_waiting(self):
        from hq.platform.application.automation_adoption import catch_up, judged
        from hq.platform.core.example_estate import automation

        automation.partly_adoptable()
        newest = self.newest()
        changes = judged(newest.pk).changes
        (refused,) = [change for change in changes if change.problems]
        self.assertGreater(len(changes), 2, "it holds changes HQ can adopt beside the one it can't")
        self.assertIn(_adopt(newest.pk), _posts(self.page()))

        response = self.client.post(_adopt(newest.pk), _sent())

        self.assertRedirects(response, reverse("control_plane:playbooks"), fetch_redirect_response=False)
        catch_up()
        self.assertEqual([change.key for change in judged(newest.pk).changes], [refused.key])
        html = self.page()
        self.assertEqual(set(_decisions(html)), {refused.key}, "only its Reject is left")
        self.assertNotIn("Adopt", _decisions(html)[refused.key])

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
        self.assertEqual(resource_capabilities(ManagedResource.objects.get(key=SOURCE_KEY)).offered, (False, False))

    def test_a_viewer_is_offered_neither_adopt_nor_reject(self):
        from hq.platform.application.roles import Role, assign

        newest = self.newest()
        viewer = operator()
        assign(viewer, Role.VIEWER)
        self.client.force_login(viewer)
        posts = _posts(self.page())
        self.assertFalse({_adopt(newest.pk), _reject(newest.pk)} & posts)

    def worker_gone(self, seen=None):
        """HQ's playbook workers hold no run, and were last seen ``seen``
        ago, or never when not given."""

        from datetime import timedelta

        from django.utils import timezone

        from hq.platform.core.models import UpstreamReading

        now = timezone.now()
        work = OperationRequest.objects.filter(action__in=("scan", "run"))
        work.filter(state=OperationRequest.State.CLAIMED).update(lease_expires_at=now - timedelta(minutes=1))
        heartbeat = UpstreamReading.objects.filter(key__startswith="automation.", key__endswith="-heartbeat")
        if seen is None:
            heartbeat.delete()
            work.update(claimed_at=None)
        else:
            heartbeat.update(observed_at=now - seen)
            work.exclude(claimed_at=None).update(claimed_at=now - seen)

    def setup_commands(self, html: str) -> str:
        return " ".join(element.words for element in with_class(html, "operator-command"))

    def test_setup_is_raised_only_while_no_worker_was_seen_within_its_interval(self):
        from datetime import timedelta

        for seen, raised in ((timedelta(minutes=20), False), (timedelta(hours=3), True), (None, True)):
            with self.subTest(seen=seen):
                self.worker_gone(seen=seen)
                self.assertEqual(bool(self.setup_commands(self.page())), raised)

    def test_a_page_read_while_the_worker_is_absent_writes_nothing(self):
        from hq.platform.core import revisions
        from hq.platform.core.models import UpstreamReading

        self.worker_gone()
        waiting = OperationRequest.objects.get(action="run", state=OperationRequest.State.QUEUED)
        tables = [model._meta.db_table for model in (UpstreamReading, OperationRequest, AuditLog)]
        before = {table: revisions.read().counts.get(table, 0) for table in tables}

        self.page()
        self.page(reverse("jobs:run", kwargs={"pk": waiting.pk}))
        self.page(reverse("jobs:list"))

        self.assertEqual({table: revisions.read().counts.get(table, 0) for table in tables}, before)
        self.assertFalse(UpstreamReading.objects.filter(key="automation-heartbeat").exists())

    def test_the_status_of_a_run_hq_never_queued_is_not_found(self):
        status = reverse("jobs:run_status", args=["00000000-0000-0000-0000-000000000000"])
        self.assertEqual(self.client.get(status).status_code, 404)

    def test_a_scan_of_the_commit_adopted_leaves_nothing_to_decide(self):
        from hq.platform.core.example_estate import automation

        automation.rescanned()
        html = self.page()
        self.assertFalse({path for path in _posts(html) if path.endswith(("/adopt/", "/reject/"))})


class EmptyPlaybooksPageTests(TestCase):
    """Before any scan: nothing about a read that never happened."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("empty") as owner:
            cls.owner = owner

    def test_where_automation_was_never_set_up_nothing_is_raised(self):
        self.client.force_login(self.owner)
        html = self.client.get(reverse("control_plane:playbooks")).content.decode()

        self.assertFalse(with_class(html, "operator-command"), "no setup is raised where nobody set automation up")


class OperationStandingTests(TestCase):
    """An operation a worker carries out stands as Jobs shows it."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def test_each_run_stands_in_the_state_jobs_shows_it_in(self):
        from django.utils import timezone

        from hq.domains.jobs.models import JobState
        from hq.platform.application.asks import DONE, FAILED, QUEUED, RUNNING
        from hq.platform.application.work_runs import (
            NOT_STARTED,
            STOPPED_REPORTING,
            run_rows,
            seen_as_runs,
            standing_of,
        )

        expected = {JobState.QUEUED: QUEUED, JobState.RUNNING: RUNNING, JobState.SUCCEEDED: DONE}
        runs = list(seen_as_runs(OperationRequest.objects.select_related("resource"), timezone.now()))
        seen = set()
        for run, row in zip(runs, run_rows(runs), strict=True):
            standing = standing_of(row)
            with self.subTest(run=str(run.pk), seen=run.seen):
                waits = row.word == NOT_STARTED
                self.assertEqual(standing.state, FAILED if waits else expected.get(JobState(run.seen), FAILED))
                self.assertEqual(standing.label, NOT_STARTED if waits else JobState(run.seen).label)
                if run.seen == JobState.LOST and run.state == OperationRequest.State.CLAIMED:
                    self.assertEqual(standing.note, STOPPED_REPORTING)
            seen.add(run.seen)
        self.assertLessEqual({JobState.QUEUED, JobState.RUNNING, JobState.SUCCEEDED, JobState.LOST}, seen)
