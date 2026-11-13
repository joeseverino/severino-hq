"""A run of a playbook has a page of its own: who asked, its approval, its
lease, how it ended and each task's outcome, and every list of runs links each
row to it. Every procedure here is a synthetic ``example.*`` one."""

import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.control_plane.models import OperationRequest
from hq.platform.application.tests.markup import all_elements, elements, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.automation import CACHE_FLUSH, CORRELATION
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media

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
        from hq.platform.application.work_runs import RUN as LISTED_RUN, hydrated, recent_runs, runs_on, work_rows

        for where, url, section, runs in (
            ("jobs", reverse("jobs:list"), "", [row for row in hydrated(work_rows()[:40]) if row.source == LISTED_RUN]),
            ("machine", reverse("control_plane:machine", kwargs={"name": MACHINE}), "runs", runs_on(MACHINE)),
            ("playbook", reverse("command", args=[RUN]), "runs", recent_runs(CACHE_FLUSH)),
        ):
            with self.subTest(page=where):
                html = self.page(url)
                drawn = all_elements(html) if not section else within(html, "id", section)
                hrefs = {element.attrs.get("href") for element in drawn if element.tag == "a"}
                self.assertTrue(runs)
                for run in runs:
                    self.assertIn(run_page(run.id), hrefs)

    def test_a_finished_run_s_page_says_who_asked_how_it_ended_and_each_task(self):
        run = OperationRequest.objects.filter(
            action=OperationRequest.Action.RUN, state=OperationRequest.State.SUCCEEDED, input__correlation=CORRELATION
        ).get()

        html = self.page(run_page(run.pk))

        said = facts(html)
        self.assertEqual(said["Asked by"][0], f"{run.requested_actor} (agent)")
        self.assertEqual(said["Result"][0], run.result["message"])
        self.assertEqual(said["Correlation"][0], CORRELATION)
        self.assertEqual(said["Machines"][1], reverse("control_plane:machine", kwargs={"name": MACHINE}))
        self.assertEqual(said["Playbook"][1], reverse("command", args=[RUN]))
        tasks = " ".join(element.words for element in within(html, "id", "tasks"))
        self.assertTrue(run.result["events"])
        for event in run.result["events"]:
            self.assertIn(event["task"], tasks)

    def test_a_working_run_s_page_names_its_lease(self):
        run = OperationRequest.objects.get(action=OperationRequest.Action.RUN, state=OperationRequest.State.CLAIMED)

        said = facts(self.page(run_page(run.pk)))

        self.assertEqual(said["Lease"][0], run.claimed_by)
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

    def test_a_run_a_person_approved_links_the_approval_it_ran_under(self):
        from hq.domains.control_plane.models import ApprovalRequest
        from hq.platform.application.security import Capability, Principal
        from hq.platform.application.tests.procedures import APPROVER, approved_run, run_payload

        agent = Principal("example-agent", "mcp", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
        answer = approved_run(self, run_payload(), principal=agent, key="example-held")
        held = ApprovalRequest.objects.get(state=ApprovalRequest.State.APPROVED)
        self.client.force_login(get_user_model().objects.create_superuser("example-owner-2", "o@example.test", "pw"))

        response = self.client.get(run_page(answer["operation"]["id"]))

        self.assertEqual(response.status_code, 200)
        entry = reverse("core:approval_entry", kwargs={"approval_id": held.pk})
        self.assertEqual(facts(response.content.decode())["Approved by"], (APPROVER, entry))


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

        offered = dict(response.context["playbooks_here"])
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

        viewer = get_user_model().objects.create_user("example-viewer")
        roles.assign(viewer, roles.Role.VIEWER)
        self.client.force_login(viewer)

        response = self.client.get(reverse("control_plane:machine", kwargs={"name": MACHINE}))

        reads = {
            capability_name(manifest.id)
            for manifest in operation_registry().values()
            if manifest.effect == "read" and manifest.admits("machine", MACHINE)
        }
        offered = {url.split("/")[2] for _, url in response.context["playbooks_here"]}
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
