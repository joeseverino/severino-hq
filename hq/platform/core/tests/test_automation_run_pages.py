"""An automation procedure's command page says what the procedure is and how
its runs went, and a machine's page lists the runs that acted on it, each
drawn by the shared section renderer with its state in its word and tone."""

import re
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse

from hq.platform.application.automation_runs import access_words
from hq.platform.application.tests.markup import links, select, text, texts, with_class, within
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
        # The commit adopted, as short as Playbooks says it.
        self.assertIn(f"Adopted commit {manifest.revision[:12]}", about)

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
        flushes = [run for run in self.runs["machine"] if run.kind == RUN]
        self.assertTrue(flushes)
        named = {element.attrs.get("href"): element.words for element in within(html, "id", "runs")}
        # Each run is named by its playbook's title, the name the Playbooks page gives it, linked to its own page.
        title = next(found["title"] for found in PROCEDURES if found["declaration"]["id"] == CACHE_FLUSH)
        for run in flushes:
            self.assertEqual(named.get(reverse("jobs:run", kwargs={"pk": run.id})), title)

    def test_a_procedure_s_page_links_each_of_its_runs_waiting_for_approval(self):
        from hq.domains.control_plane.models import ApprovalRequest

        held = ApprovalRequest.objects.filter(
            state=ApprovalRequest.State.PENDING, capability__startswith="automation.", capability__endswith=".run"
        )
        self.assertTrue(held)
        for request in held:
            with self.subTest(capability=request.capability):
                html = self.page(reverse("command", args=[request.capability]))
                decided_at = reverse("core:approval_entry", kwargs={"approval_id": request.id})
                self.assertIn(
                    ("Waiting for approval", decided_at),
                    [(element.words, element.attrs.get("href")) for element in within(html, "id", "held-runs")],
                )

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

    def test_each_procedure_s_inputs_are_its_form_s_fields_said_once(self):
        """Each input its argument spec declares is a field of the run's
        form, its description said once on the page: no table beside the
        form repeats them under their raw names."""

        from hq.platform.application.automation_adoption import procedures

        adopted = [procedure for procedure in procedures() if procedure.inputs]
        self.assertTrue(adopted)
        for procedure in adopted:
            html = self.page(procedure.id)
            for name, spec in procedure.inputs.items():
                with self.subTest(procedure=procedure.id, input=name):
                    self.assertTrue(select(html, "name", f"parameters.{name}"))
                    if spec.description:
                        self.assertEqual(text(html).count(spec.description), 1)

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

    def test_an_agent_reads_whether_its_run_waits_and_the_page_says_none_of_it(self):
        """Whether an agent's run waits for a person is in the notes
        describe_capabilities answers, and on no page."""

        from hq.platform.application.approvals import no_rule_allows
        from hq.platform.application.automation_adoption import playbook_titles
        from hq.platform.application.capabilities import capability_registry

        waits_note = "When an agent asks, the run waits for an operator's approval."
        waits = set()
        for operation_id in playbook_titles():
            with self.subTest(procedure=operation_id):
                spec = capability_registry()[f"automation.{operation_id}.run"]
                waits.add(no_rule_allows(spec))
                self.assertEqual(waits_note in spec.execution_notes, no_rule_allows(spec))
                page = self.page(operation_id)
                self.assertFalse(with_class(page, "page-lede"))
                self.assertNotIn(spec.summary, text(page))
        self.assertEqual(waits, {True, False})

    def test_a_held_run_s_card_says_what_approving_it_runs(self):
        """The card names the playbook by its title, and shows the machines,
        what the run does, its access and the commit adopted."""

        from hq.domains.control_plane.models import ApprovalRequest
        from hq.platform.application.approval_review import review
        from hq.platform.application.automation_adoption import playbook_titles
        from hq.platform.application.automation_runs import operation_registry

        held = ApprovalRequest.objects.filter(
            state=ApprovalRequest.State.PENDING, capability__startswith="automation.", capability__endswith=".run"
        )
        self.assertTrue(held)
        for request in held:
            with self.subTest(capability=request.capability):
                operation = request.baseline["spec"]["operation"]
                manifest = operation_registry()[operation]
                card = review(request)
                self.assertIn(f"“{playbook_titles()[operation]}”", str(card["asked"]))
                shown = {row.label: row.after for row in card["preview"].rows}
                for host in request.baseline["hosts"]:
                    self.assertIn(host, shown["Machines"])
                self.assertEqual(shown["Adopted commit"], manifest.revision[:12])
                self.assertIn("Effect", shown)
                self.assertIn("sudo", shown["Access"])

    def test_each_procedure_s_page_leads_back_to_playbooks(self):
        from hq.platform.application.automation_adoption import procedures

        home = reverse("control_plane:procedures")
        for procedure in procedures():
            with self.subTest(procedure=procedure.id):
                html = self.page(procedure.id)
                trail = [element.attrs["href"] for element in within(html, "class", "page-trail") if element.tag == "a"]
                self.assertEqual(trail, [home])
                self.assertEqual([href for words, href in links(html) if words == "Cancel"], [home])

    def test_each_procedure_s_page_says_its_access_in_plain_words(self):
        """Whether a run escalates, and through which profile, as sudo."""

        from hq.platform.application.automation_runs import operation_registry

        manifests = operation_registry().values()
        self.assertEqual({manifest.privilege is None for manifest in manifests}, {True, False})
        for manifest in manifests:
            with self.subTest(procedure=manifest.id):
                about = " ".join(element.words for element in select(self.page(manifest.id), "id", "command-about"))
                self.assertIn("Access", about)
                if manifest.privilege is None:
                    self.assertIn("no sudo", about.lower())
                else:
                    self.assertIn(access_words(manifest.privilege), about)

    def test_a_procedure_runs_on_the_machines_it_names_each_by_its_link_and_else_on_any(self):
        from hq.platform.application.automation_runs import about, operation_registry
        from hq.platform.application.entity_links import entity_link
        from hq.platform.application.security import Capability, Principal

        declared = operation_registry()[CACHE_FLUSH]
        reader = Principal("example-reader", "web", frozenset({Capability.READ}))

        def runs_on(manifest):
            (fact,) = [fact for fact in about(manifest, reader).facts if fact.label == "Runs on"]
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
