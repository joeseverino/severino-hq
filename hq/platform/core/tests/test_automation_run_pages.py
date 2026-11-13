"""An automation procedure's command page says what the procedure is and how
its runs went, and a machine's page lists the runs that acted on it, each
drawn by the shared section renderer with its state in its word and tone."""

import re
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from hq.platform.application.tests.markup import links, select, texts, within
from hq.platform.application.tests.source import source_files, template_files
from hq.platform.application.tests.tree import ROOT
from hq.platform.core import seeds
from hq.platform.core.example_estate.automation import CACHE_FLUSH, PROCEDURES, REVISION
from hq.platform.core.example_estate.names import MACHINE
from hq.platform.core.example_estate.testing import throwaway_media

RUN = f"automation.{CACHE_FLUSH}.run"


def _statuses(html, section: str) -> list[tuple[str, str]]:
    """Each row state a section draws, as its words and its tone class: never
    the outcomes in a run's evidence beneath it."""

    return [
        (element.words, next(name for name in element.classes if name.startswith("pill-")))
        for element in within(html, "id", section)
        if "pill" in element.classes and not any("row-below" in a.classes for a in (*element.ancestors, element))
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
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        self.client.force_login(self.owner)

    def page(self, url: str) -> str:
        from hq.platform.application.work_runs import recent_runs, runs_on

        response = self.client.get(url)
        self.runs = {"command": recent_runs(CACHE_FLUSH), "machine": runs_on(MACHINE)}
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_a_procedure_s_page_states_what_its_adopted_catalog_declares(self):
        from hq.platform.application.automation_runs import operation_registry

        html = self.page(reverse("command", args=[RUN]))
        manifest = operation_registry()[CACHE_FLUSH]
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
        # Each run is named by its playbook's title, the name the Playbooks page gives it.
        title = next(found["title"] for found in PROCEDURES if found["declaration"]["id"] == CACHE_FLUSH)
        self.assertEqual({element.words for element in procedure}, {title})

    def test_a_command_with_nothing_to_say_of_itself_draws_no_more_than_its_form(self):
        html = self.page(reverse("command", args=["infrastructure.controller.refresh"]))

        self.assertEqual(select(html, "id", "command-about"), [])
        self.assertEqual(select(html, "id", "runs"), [])


class CommandAboutTests(SimpleTestCase):
    def test_facts_are_drawn_under_a_heading_so_they_need_one(self):
        from hq.platform.application.integration_specs import CommandAbout, CommandFact

        with self.assertRaisesRegex(ValueError, "label"):
            CommandAbout(facts=(CommandFact("Runs on", "Any machine"),))


class ProcedurePageTests(TestCase):
    """A procedure's command page says what the adopted catalog declares of it."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        from hq.platform.application.automation_adoption import catch_up

        # Outside a request, this process reads the catalog seeded for the test.
        catch_up()
        self.client.force_login(self.owner)

    def page(self, operation_id: str) -> str:
        response = self.client.get(reverse("command", args=[f"automation.{operation_id}.run"]))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def test_each_procedure_s_page_lists_the_inputs_its_argument_spec_declares(self):
        from hq.platform.application.automation_adoption import procedures

        adopted = procedures()
        self.assertTrue(adopted)
        for procedure in adopted:
            html = self.page(procedure.id)
            rows = [element.words for element in within(html, "id", "inputs") if element.tag == "tr"]
            for name, spec in procedure.inputs.items():
                with self.subTest(procedure=procedure.id, input=name):
                    (row,) = [row for row in rows if row.split()[:1] == [name]]
                    for said in (spec.description, *map(str, spec.choices)):
                        self.assertIn(said, row)
                    if spec.bound is not None:
                        self.assertIn(spec.bound.pattern, row)
                        self.assertIn(str(spec.bound.max_length), row)

    def test_each_procedure_s_page_is_named_by_its_playbook_s_title(self):
        """The heading and the button say the title Playbooks gives the
        playbook, and a held run's sentence names it so too."""

        from hq.platform.application.automation_adoption import playbook_titles
        from hq.platform.application.capabilities import capability_registry

        titles = playbook_titles()
        self.assertTrue(titles)
        for operation_id, title in titles.items():
            with self.subTest(procedure=operation_id):
                html = self.page(operation_id)
                self.assertEqual(texts(html, "h1"), [title])
                buttons = [
                    element.words
                    for element in within(html, "data-command-form")
                    if element.attrs.get("type") == "submit"
                ]
                self.assertEqual(buttons, [title])
                self.assertIn(title, capability_registry()[f"automation.{operation_id}.run"].wanted())

    def test_each_procedure_s_page_leads_back_to_playbooks(self):
        from hq.platform.application.automation_adoption import procedures

        home = reverse("control_plane:procedures")
        for procedure in procedures():
            with self.subTest(procedure=procedure.id):
                html = self.page(procedure.id)
                trail = [element.attrs["href"] for element in within(html, "class", "page-trail") if element.tag == "a"]
                self.assertEqual(trail, [home])
                self.assertEqual([href for words, href in links(html) if words == "Cancel"], [home])

    def test_each_procedure_s_page_names_its_privilege(self):
        from hq.platform.application.automation_runs import operation_registry

        for manifest in operation_registry().values():
            with self.subTest(procedure=manifest.id):
                about = select(self.page(manifest.id), "id", "command-about")
                self.assertIn(manifest.privilege or "None", " ".join(element.words for element in about))

    def test_a_procedure_runs_on_the_machines_it_names_each_by_its_link_and_else_on_any(self):
        from hq.platform.application.automation_adoption import generation
        from hq.platform.application.automation_runs import about, operation_registry
        from hq.platform.application.entity_links import entity_link
        from hq.platform.application.security import Capability, Principal

        declared = operation_registry()[CACHE_FLUSH]
        reader = Principal("example-reader", "web", frozenset({Capability.READ}))

        def runs_on(manifest):
            (fact,) = [fact for fact in about(manifest, generation(), reader).facts if fact.label == "Runs on"]
            return fact

        named = declared.model_copy(update={"hosts": (MACHINE,)})
        self.assertEqual(runs_on(named).links, (entity_link("machine", MACHINE),))
        self.assertFalse(declared.hosts)
        self.assertEqual(runs_on(declared).links, ())

    def test_a_procedure_s_record_lives_on_its_command_page_while_the_catalog_names_it(self):
        from hq.domains.control_plane.models import ManagedResource
        from hq.platform.application.automation_runs import resource_key

        detail = reverse("control_plane:detail", kwargs={"key": resource_key(CACHE_FLUSH)})
        self.assertRedirects(self.client.get(detail), reverse("command", args=[RUN]))

        # A record the catalog does not name keeps its own page.
        record = ManagedResource.objects.get(key=resource_key(CACHE_FLUSH))
        dropped = ManagedResource.objects.create(
            key=resource_key("example.retired"), kind=record.kind, spec={**record.spec, "operation": "example.retired"}
        )
        self.assertEqual(self.client.get(reverse("control_plane:detail", kwargs={"key": dropped.key})).status_code, 200)


@tag("source", "tree")
class NoProcedureNamedTests(SimpleTestCase):
    """The host draws a procedure from the adopted catalog alone: no template
    and no module outside the example estate and the tests names one."""

    def test_no_template_or_module_names_a_seeded_procedure(self):
        named = {REVISION}
        for procedure in PROCEDURES:
            named |= {procedure["declaration"]["id"], procedure["playbook"], procedure["sidecar"]}
        needle = re.compile("|".join(re.escape(word) for word in sorted(named)))
        modules = [
            path
            for path in source_files(ROOT / "hq", "*.py")
            if not {"tests", "example_estate"} & set(path.relative_to(ROOT).parts)
        ]
        self.assertTrue(modules)
        offences = [
            f"{path.relative_to(ROOT)}: {found.group(0)}"
            for path in (*modules, *(path for _name, path in template_files()))
            if (found := needle.search(path.read_text()))
        ]
        self.assertEqual(offences, [], "a procedure is named in the host; draw it from the adopted catalog")
