"""A page that lists automation runs shows their evidence only to a reader who
may open Jobs. Every procedure here is a synthetic ``example.*`` one."""

from unittest import mock

from django.test import TestCase
from django.urls import reverse

from hq.platform.application import roles
from hq.platform.application.markup_testing import all_elements, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.automation import CACHE_FLUSH
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.testing import operator

# A capability no role grants.
UNHELD = "example.unheld"


def _evidence(html, section: str = "") -> dict[str, list[str]]:
    """The words of each labeled block a row's open line shows, by its label,
    within the section ``section`` or anywhere."""

    found: dict[str, list[str]] = {}
    for element in all_elements(html):
        if "row-below" not in element.classes:
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
        cls.viewer = operator()
        roles.assign(cls.viewer, roles.Role.VIEWER)

    def page(self, user, url: str) -> str:
        self.client.force_login(user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

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
