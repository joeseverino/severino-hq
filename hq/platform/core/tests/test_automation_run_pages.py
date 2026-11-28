"""An automation procedure's command page: a field for each input it declares,
what approving a held run runs, where it runs, and its record's page."""

from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.platform.application.markup_testing import select
from hq.platform.core import seeds
from hq.platform.core.example_estate.automation import CACHE_FLUSH
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.testing import declare

RUN = f"automation.{CACHE_FLUSH}.run"


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

    def test_each_procedure_s_inputs_are_its_form_s_fields(self):
        from hq.platform.application.automation_adoption import procedures

        adopted = [procedure for procedure in procedures() if procedure.inputs]
        self.assertTrue(adopted)
        for procedure in adopted:
            html = self.page(procedure.id)
            for name in procedure.inputs:
                with self.subTest(procedure=procedure.id, input=name):
                    self.assertTrue(select(html, "name", f"parameters.{name}"))

    def test_an_agent_reads_whether_its_run_waits(self):
        """Whether an agent's run waits for a person is in the notes describe_capabilities answers."""

        from hq.platform.application.approvals import no_rule_allows
        from hq.platform.application.automation_adoption import playbook_titles
        from hq.platform.application.capabilities import command_spec

        waits_note = "When an agent asks, the run waits for an operator's approval."
        waits = set()
        for operation_id in playbook_titles():
            with self.subTest(procedure=operation_id):
                spec = command_spec(f"automation.{operation_id}.run")
                waits.add(no_rule_allows(spec))
                self.assertEqual(waits_note in spec.execution_notes, no_rule_allows(spec))
        self.assertEqual(waits, {True, False})

    def test_a_held_run_s_card_says_what_approving_it_runs(self):
        """The card names the playbook by its title in a row of its own, and
        shows the machines, what the run does, its access and the commit adopted."""

        from hq.domains.control_plane.models import ApprovalRequest
        from hq.platform.application.approval_review import review
        from hq.platform.application.automation_adoption import playbook_titles
        from hq.platform.application.automation_runs import operation_registry
        from hq.platform.application.security import cli_principal

        held = ApprovalRequest.objects.filter(
            state=ApprovalRequest.State.PENDING, capability__startswith="automation.", capability__endswith=".run"
        )
        self.assertTrue(held)
        for request in held:
            with self.subTest(capability=request.capability):
                operation = request.baseline["spec"]["operation"]
                manifest = operation_registry()[operation]
                card = review(request, cli_principal())
                shown = {row.label: row.after for row in card["preview"].rows}
                self.assertEqual(shown["Playbook"], playbook_titles()[operation])
                for host in request.baseline["hosts"]:
                    self.assertIn(host, shown["Machines"])
                self.assertEqual(shown["Adopted commit"], manifest.revision[:12])
                self.assertIn("Effect", shown)
                self.assertIn("sudo", shown["Access"])

    def test_a_procedure_s_record_lives_on_its_command_page_while_the_catalog_names_it(self):
        from hq.domains.control_plane.models import ManagedResource
        from hq.platform.application.automation_runs import resource_key

        detail = reverse("control_plane:detail", kwargs={"key": resource_key(CACHE_FLUSH)})
        self.assertRedirects(self.client.get(detail), reverse("command", args=[RUN]))

        # A record the catalog does not name keeps its own page.
        record = ManagedResource.objects.get(key=resource_key(CACHE_FLUSH))
        dropped = declare(resource_key("example.retired"), record.kind, {**record.spec, "operation": "example.retired"})
        self.assertEqual(self.client.get(reverse("control_plane:detail", kwargs={"key": dropped.key})).status_code, 200)
