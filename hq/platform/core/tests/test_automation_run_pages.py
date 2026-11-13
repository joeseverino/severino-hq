"""An automation procedure's command page says what the procedure is and how
its runs went, and a machine's page lists the runs that acted on it, each
drawn by the shared section renderer with its state in its word and tone."""

from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.platform.application.tests.example_procedures import supplied
from hq.platform.application.tests.markup import select, within
from hq.platform.core import seeds
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media
from tests.fixtures.example_hq_plugin.operations import CACHE_FLUSH, operations

RUN = f"automation.{CACHE_FLUSH}.run"


def _statuses(html, section: str) -> list[tuple[str, str]]:
    """Each status a section draws, as its words and its tone class."""

    return [
        (element.words, next(name for name in element.classes if name.startswith("pill-")))
        for element in within(html, "id", section)
        if "pill" in element.classes
    ]


def _more(html, section: str, path: str) -> dict[str, list[str]]:
    """The query of the one link in a section that goes to ``path``."""

    (found,) = [
        urlsplit(element.attrs["href"])
        for element in within(html, "id", section)
        if element.tag == "a" and urlsplit(element.attrs["href"]).path == path
    ]
    return parse_qs(found.query)


class AutomationRunPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with supplied(), throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, url: str) -> str:
        with supplied():
            from hq.platform.application.work_runs import recent_runs, runs_on

            response = self.client.get(url)
            self.runs = {"command": recent_runs(CACHE_FLUSH), "machine": runs_on(MACHINE)}
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_procedure_s_page_states_what_its_manifest_declares(self):
        html = self.page(reverse("command", args=[RUN]))
        manifest = next(manifest for manifest in operations() if manifest.id == CACHE_FLUSH)
        about = " ".join(element.words for element in select(html, "id", "command-about"))

        for statement in (*manifest.preconditions, *manifest.verification, *manifest.recovery):
            with self.subTest(statement=statement):
                self.assertIn(statement, about)
        self.assertIn(manifest.revision, about)

    def test_a_procedure_s_page_lists_its_latest_runs_in_their_words_and_tones(self):
        html = self.page(reverse("command", args=[RUN]))

        self.assertTrue(self.runs["command"])
        self.assertEqual(_statuses(html, "runs"), [(run.word, f"pill-{run.tone}") for run in self.runs["command"]])
        self.assertEqual(_more(html, "runs", reverse("jobs:list")), {"kind": [RUN]})

    def test_a_machine_s_page_lists_the_runs_that_acted_on_it(self):
        html = self.page(reverse("control_plane:machine", kwargs={"name": MACHINE}))

        self.assertTrue(self.runs["machine"])
        self.assertEqual(_statuses(html, "runs"), [(run.word, f"pill-{run.tone}") for run in self.runs["machine"]])
        self.assertEqual(_more(html, "runs", reverse("jobs:list")), {"host": [MACHINE]})
        procedure = [
            element
            for element in within(html, "id", "runs")
            if element.attrs.get("href") == reverse("command", args=[RUN])
        ]
        self.assertTrue(procedure)

    def test_a_command_with_nothing_to_say_of_itself_draws_no_more_than_its_form(self):
        html = self.page(reverse("command", args=["infrastructure.controller.refresh"]))

        self.assertEqual(select(html, "id", "command-about"), [])
        self.assertEqual(select(html, "id", "runs"), [])


class CommandAboutTests(SimpleTestCase):
    def test_facts_are_drawn_under_a_heading_so_they_need_one(self):
        from hq.platform.application.integration_specs import CommandAbout, CommandFact

        with self.assertRaisesRegex(ValueError, "label"):
            CommandAbout(facts=(CommandFact("Runs on", "Any machine"),))
