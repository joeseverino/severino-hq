"""Every page that lists automation runs shows the same evidence, drawn by one
part, to a reader who may open Jobs, and to no one else. Every procedure here
is a synthetic ``example.*`` one."""

from unittest import mock
from urllib.parse import urlencode

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.platform.application import roles
from hq.platform.application.tests.markup import all_elements, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.automation import CACHE_FLUSH, CORRELATION
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media

# A capability no role grants.
UNHELD = "example.unheld"


def _evidence(html, section: str = "") -> dict[str, list[str]]:
    """The words of each labeled block a row's open line shows, by its label,
    within the section ``section`` or anywhere."""

    found: dict[str, list[str]] = {}
    for element in all_elements(html):
        if "row-below" not in element.classes or not element.beneath:
            continue
        if section and not any(ancestor.attrs.get("id") == section for ancestor in element.ancestors):
            continue
        found.setdefault(element.attrs.get("data-label", ""), []).append(element.words)
    return found


def _states(html, section: str) -> list[str]:
    """The words of each status a section's rows draw, outside what they hold back."""

    return [
        element.words
        for element in within(html, "id", section)
        if "pill" in element.classes
        and not any("row-below" in ancestor.classes for ancestor in (*element.ancestors, element))
    ]


class RunEvidencePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner
        cls.viewer = get_user_model().objects.create_user("example-viewer")
        roles.assign(cls.viewer, roles.Role.VIEWER)

    def page(self, user, url: str) -> str:
        self.client.force_login(user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_jobs_and_a_machine_s_page_draw_the_same_evidence(self):
        from hq.platform.application.work_runs import runs_on

        machine = self.page(self.owner, reverse("control_plane:machine", kwargs={"name": MACHINE}))
        jobs = self.page(self.owner, f"{reverse('jobs:list')}?{urlencode({'host': MACHINE})}")

        tasks = [event["task"] for run in runs_on(MACHINE) for event in run.evidence.events]
        self.assertTrue(tasks)
        for page, drawn in (("machine", _evidence(machine, "runs")), ("jobs", _evidence(jobs))):
            with self.subTest(page=page):
                self.assertIn(CORRELATION, " ".join(drawn.get("Correlation", ())))
                self.assertTrue(drawn.get("Approved by"))
                self.assertTrue(drawn.get("Lease"))
                for task in tasks:
                    self.assertTrue(any(task in said for said in drawn.get("Tasks", ())), task)

    def test_a_procedure_s_page_draws_its_runs_evidence(self):
        from hq.platform.application.work_runs import recent_runs

        html = self.page(self.owner, reverse("command", args=[f"automation.{CACHE_FLUSH}.run"]))

        drawn = _evidence(html, "runs")
        tasks = [event["task"] for run in recent_runs(CACHE_FLUSH) for event in run.evidence.events]
        self.assertTrue(tasks)
        self.assertIn(CORRELATION, " ".join(drawn.get("Correlation", ())))
        for task in tasks:
            self.assertTrue(any(task in said for said in drawn.get("Tasks", ())), task)

    def test_a_reader_who_may_not_open_jobs_sees_the_runs_but_not_their_evidence(self):
        from hq.domains.jobs.views import JobListView

        # The machine page is a viewer's to read, a procedure's page only an
        # operator's; Jobs is made to need what neither holds.
        for page, user, url in (
            ("machine", self.viewer, reverse("control_plane:machine", kwargs={"name": MACHINE})),
            ("procedure", self.owner, reverse("command", args=[f"automation.{CACHE_FLUSH}.run"])),
        ):
            with self.subTest(page=page):
                allowed = self.page(user, url)
                with mock.patch.object(JobListView, "required_capability", UNHELD, create=True):
                    refused = self.page(user, url)
                    self.assertEqual(self.client.get(reverse("jobs:list")).status_code, 403)

                self.assertTrue(_states(refused, "runs"))
                self.assertEqual(_states(refused, "runs"), _states(allowed, "runs"))
                self.assertTrue(_evidence(allowed, "runs"))
                self.assertEqual(_evidence(refused, "runs"), {})
                asker = self.owner.get_username()
                self.assertNotIn(asker, " ".join(element.words for element in within(refused, "id", "runs")))
                self.assertIn(asker, " ".join(element.words for element in within(allowed, "id", "runs")))
