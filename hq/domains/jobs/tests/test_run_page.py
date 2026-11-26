"""A run of a playbook has a page of its own: who asked, its approval, its
lease, how it ended and each task's outcome, and every list of runs links each
row to it. Every procedure here is a synthetic ``example.*`` one."""

import uuid
from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import OperationRequest
from hq.platform.application.markup_testing import all_elements, elements, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.automation import CACHE_FLUSH, CORRELATION
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.testing import AGENT, operator

RUN = f"automation.{CACHE_FLUSH}.run"
# A capability no role grants.
UNHELD = "example.unheld"


def facts(html) -> dict[str, tuple[str, str]]:
    """Each fact a page states, by its label: its value's words and the link it is, if any."""

    found: dict[str, tuple[str, str]] = {}
    elements = all_elements(html)
    for fact in (element for element in elements if "fact" in element.classes):
        inside = [element for element in elements if any(ancestor is fact for ancestor in element.ancestors)]
        label = next(element.words for element in inside if "fact-label" in element.classes)
        value = next(element for element in inside if "fact-value" in element.classes)
        link = next((element.attrs["href"] for element in inside if element.tag == "a"), "")
        found[label] = (value.words, link)
    return found


def run_page(pk) -> str:
    return reverse("jobs:run", kwargs={"pk": pk})


class RunPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, url: str) -> str:
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_every_list_of_runs_links_each_run_to_its_page(self):
        from hq.platform.application.resources import list_resource
        from hq.platform.application.security import web_principal
        from hq.platform.application.work_runs import RUN as LISTED_RUN, recent_runs, runs_on

        page = list_resource("work", {"limit": 40}, principal=web_principal(self.owner))["items"]
        for where, url, section, runs in (
            ("jobs", reverse("jobs:list"), "", [row["id"] for row in page if row["source"] == LISTED_RUN]),
            (
                "machine",
                reverse("control_plane:machine", kwargs={"name": MACHINE}),
                "runs",
                [row.id for row in runs_on(MACHINE)],
            ),
            ("playbook", reverse("command", args=[RUN]), "runs", [row.id for row in recent_runs(CACHE_FLUSH)]),
        ):
            with self.subTest(page=where):
                html = self.page(url)
                drawn = all_elements(html) if not section else within(html, "id", section)
                hrefs = {element.attrs.get("href") for element in drawn if element.tag == "a"}
                self.assertTrue(runs)
                for run in runs:
                    self.assertIn(run_page(run), hrefs)

    def test_a_finished_run_s_page_links_its_machines_and_playbook(self):
        run = OperationRequest.objects.filter(
            action=OperationRequest.Action.RUN, state=OperationRequest.State.SUCCEEDED, input__correlation=CORRELATION
        ).get()

        said = facts(self.page(run_page(run.pk)))

        self.assertEqual(said["Machines"][1], reverse("control_plane:machine", kwargs={"name": MACHINE}))
        self.assertEqual(said["Playbook"][1], reverse("command", args=[RUN]))

    def test_a_scan_s_page_names_no_playbook_and_decides_nothing(self):
        scan = OperationRequest.objects.filter(action=OperationRequest.Action.SCAN).exclude(proposal=None).first()

        html = self.page(run_page(scan.pk))

        self.assertNotIn("Playbook", facts(html), "a scan runs no playbook")
        posts = {element.attrs.get("formaction", "") for element in all_elements(html)}
        self.assertFalse({path for path in posts if path.endswith(("/adopt/", "/reject/"))})

    def test_a_working_run_s_page_names_its_worker_in_plain_words(self):
        run = OperationRequest.objects.get(action=OperationRequest.Action.RUN, state=OperationRequest.State.CLAIMED)

        said = facts(self.page(run_page(run.pk)))

        self.assertIn(run.claimed_by, said["Claimed by"][0])
        self.assertNotIn("Lease", said)
        self.assertNotIn("Result", said)

    def test_a_reader_who_may_not_open_jobs_is_refused_a_run_s_page_and_offered_no_link_to_one(self):
        from hq.domains.jobs.views import JobListView, RunView
        from hq.platform.application.work_runs import runs_on

        run = runs_on(MACHINE)[0]
        with (
            mock.patch.object(JobListView, "required_capability", UNHELD, create=True),
            mock.patch.object(RunView, "required_capability", UNHELD, create=True),
        ):
            self.assertEqual(self.client.get(run_page(run.id)).status_code, 403)
            html = self.page(reverse("control_plane:machine", kwargs={"name": MACHINE}))

        hrefs = {element.attrs.get("href") for element in within(html, "id", "runs")}
        self.assertNotIn(run_page(run.id), hrefs)
        self.assertIn(reverse("command", args=[RUN]), hrefs)

    def test_an_id_no_run_has_is_not_found(self):
        from hq.domains.jobs.models import Job

        job = Job.objects.create(kind="example.job")
        for pk in (uuid.uuid4(), job.pk):
            with self.subTest(pk=pk):
                self.assertEqual(self.client.get(run_page(pk)).status_code, 404)


class ApprovedRunPageTests(TestCase):
    def setUp(self):
        from hq.platform.application.tests.procedures import Supply, manifest

        Supply(self, manifest())

    def test_a_run_a_person_approved_folds_the_chain_it_ran_in_with_its_approval(self):
        from hq.domains.control_plane.models import ApprovalRequest
        from hq.platform.application.markup_testing import hrefs
        from hq.platform.application.tests.procedures import APPROVER, approved_run, run_payload
        from hq.platform.core.models import AuditLog

        answer = approved_run(self, run_payload(), principal=AGENT, key="example-held")
        held = ApprovalRequest.objects.get(state=ApprovalRequest.State.APPROVED)
        operator(self.client)

        response = self.client.get(run_page(answer["operation"]["id"]))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        entry = reverse("core:approval_entry", args=[held.pk])
        _, opens = facts(html)["Asked by"]
        self.assertIn(f"approved by {APPROVER}", " ".join(element.words for element in all_elements(html)))
        # Who approved it opens the approval's own entry.
        self.assertEqual(opens, entry)
        # The approval is a step of the fold, opening its entry, which links the whole chain on the audit log.
        asked = AuditLog.objects.get(object_id=str(held.pk), action=AuditLog.Action.CREATED)
        (events,) = response.context["events"]
        (approving,) = [step for step in events.lines if asked in step.events]
        self.assertEqual(approving.url, entry)
        self.assertIn(entry, hrefs(html))
        self.assertIn(f"{reverse('core:audit_list')}?chain={asked.trace}", hrefs(html))
        # The approval's line names the person who decided it, alone.
        self.assertEqual((approving.who.actor, approving.who.approved_by), (APPROVER, ""))
        # Its heading counts every row of history the table draws.
        drawn = [line for event in events.lines for line in event.lines if line.rest is None]
        self.assertEqual(events.count, len(drawn))
        self.assertGreater(events.count, len(events.lines))


class LiveRunPageTests(TestCase):
    """While a run runs, its page's run_status region shows the task under way
        and each task so far as its worker's renewals say them, and asks its page
        again until the run ends; then each record of what its playbook stated, in
    the words its kind declares, with the records as stated under Raw data."""

    WORKER = "example-worker"

    def setUp(self):
        from hq.platform.application.tests.procedures import Supply, manifest

        Supply(self, manifest())

    def worker(self, action: str, payload=None, **query):
        """One call of the playbook worker, on its own socket."""

        from hq.domains.control_plane.tests import bridge_client

        return bridge_client.call(action, payload, app=bridge_client.worker(), controller_id=self.WORKER, **query)

    def region(self, pk) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        """The facts the run_status region states, and its attributes."""

        response = self.client.get(run_page(pk))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        (region,) = (
            element for element in all_elements(html) if element.attrs.get("data-fragment-name") == "run_status"
        )
        return facts(html), region.attrs

    def test_the_task_under_way_and_each_task_so_far_show_as_the_worker_says_them(self):
        from hq.platform.application.tests.procedures import APP, approved_run, run_payload

        operation = approved_run(self, run_payload(), principal=AGENT, key="example-live")["operation"]["id"]
        claimed = self.worker("claim", capability=["automation.operation:run"], lease_seconds=120, attempt="example-1")
        self.assertEqual(claimed["operation"]["id"], operation)
        hello = {"host": APP, "task": "Say hello", "outcome": "ok", "message": "Hello."}
        touching = {"host": APP, "task": "Touch a file", "outcome": "started", "message": ""}
        self.worker("renew", {"events": [hello, touching]}, operation=operation, lease_seconds=120)
        operator(self.client)

        running, attributes = self.region(operation)

        self.assertIn("Touch a file", running["Now"][0])
        self.assertIn("Say hello", running["Tasks"][0])
        self.assertIn("Duration", running)
        self.assertTrue(attributes.get("data-fragment-poll"))

        touched = {**touching, "outcome": "changed", "message": "Touched."}
        self.worker("renew", {"events": [hello, touched]}, operation=operation, lease_seconds=120)
        pulled = {"project": "example-app", "result": "rolled_back", "health": "unhealthy", "changed": True}
        report = {
            "success": True,
            "observed_generation": claimed["input"]["generation"],
            "message": "Done.",
            "stated": {"readings": {APP: {"apps.pull": [pulled]}}},
        }
        self.worker("report", report, operation=operation)

        ended, attributes = self.region(operation)

        self.assertNotIn("Now", ended)
        self.assertIn("Touched.", ended["Tasks"][0])
        readings = ended["Readings"][0]
        self.assertIn("example-app", readings)
        self.assertIn("Rolled back", readings)
        self.assertNotIn("rolled_back", readings)
        html = self.client.get(run_page(operation)).content.decode()
        raw = [element.text for element in all_elements(html) if element.tag == "pre"]
        self.assertTrue(any('"rolled_back"' in text for text in raw), raw)
        self.assertNotIn("data-fragment-poll", attributes)


class RunHereTests(TestCase):
    """A machine's page offers each playbook that may run on it, its Run page
    opened with the machine chosen, and lists its runs above what it holds."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def test_the_machine_s_page_offers_each_playbook_that_runs_there_with_it_chosen(self):
        from hq.platform.application.automation_runs import operation_registry

        response = self.client.get(reverse("control_plane:machine", kwargs={"name": MACHINE}))

        offered = {each.title: each.url for each in response.context["playbooks_here"]}
        self.assertTrue(offered)
        runnable = [manifest for manifest in operation_registry().values() if manifest.admits("machine", MACHINE)]
        self.assertEqual(len(offered), len(runnable))
        hrefs = {element.attrs.get("href") for element in all_elements(response.content.decode())}
        for url in offered.values():
            self.assertIn(url, hrefs)
            chosen = self.client.get(url).context["form"]["hosts"].value()
            self.assertEqual(list(chosen), [MACHINE])

    def test_a_reader_who_may_only_read_is_offered_only_the_playbooks_that_read(self):
        from hq.platform.application import roles
        from hq.platform.application.automation_runs import capability_name, operation_registry

        viewer = operator()
        roles.assign(viewer, roles.Role.VIEWER)
        self.client.force_login(viewer)

        response = self.client.get(reverse("control_plane:machine", kwargs={"name": MACHINE}))

        reads = {
            capability_name(manifest.id)
            for manifest in operation_registry().values()
            if manifest.effect == "read" and manifest.admits("machine", MACHINE)
        }
        offered = {each.capability for each in response.context["playbooks_here"]}
        self.assertTrue(reads)
        self.assertEqual(offered, reads)

    def test_a_link_chooses_only_a_machine_the_form_offers(self):
        response = self.client.get(reverse("command", args=[RUN]), {"hosts": ["unknown.example.com", MACHINE]})

        self.assertEqual(list(response.context["form"]["hosts"].value()), [MACHINE])
        refused = self.client.get(reverse("command", args=[RUN]), {"hosts": "unknown.example.com"})
        self.assertFalse(refused.context["form"]["hosts"].value())

    def test_the_machine_s_runs_come_before_its_containers(self):
        html = self.client.get(reverse("control_plane:machine", kwargs={"name": MACHINE})).content.decode()

        headings = [element.words for element in elements(html, "h2")]
        runs = next(at for at, words in enumerate(headings) if words.startswith("Recent runs"))
        containers = next(at for at, words in enumerate(headings) if words.startswith("Containers"))
        self.assertLess(runs, containers)

    def test_the_audit_log_titles_a_run_and_a_scan_as_jobs_does(self):
        """Each run of a playbook still adopted, whose title is the same now as when it was asked."""

        from hq.platform.application.automation_adoption import playbook_titles
        from hq.platform.application.work_runs import RUN as LISTED_RUN, hydrated, work
        from hq.platform.core.models import AuditLog

        titled = {*playbook_titles().values(), OperationRequest.Action.SCAN.label}
        listed = {
            row.id: row.title for row in hydrated(work().kept()) if row.source == LISTED_RUN and row.title in titled
        }
        asked = dict(
            AuditLog.objects.filter(object_id__in=listed, action=AuditLog.Action.CREATED).values_list(
                "object_id", "object_repr"
            )
        )

        self.assertTrue(asked)
        for pk, title in asked.items():
            self.assertEqual(title, listed[pk])
