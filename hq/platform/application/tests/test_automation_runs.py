"""A procedure is run through its own command, at HQ's one checkpoint, and
queued for the automation worker; nothing runs. Every manifest here is a
synthetic ``example.*`` one, supplied as an extension supplies it."""

import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings

from hq.domains.control_plane.models import ApprovalRequest, CapabilityRule, ManagedResource, OperationRequest
from hq.platform.application.markup_testing import links, text
from hq.platform.core.models import AgentIdentity
from hq.platform.core.testing import AGENT, MCP_READER, operator

from ..automation_runs import CANCEL
from ..capabilities import capability_registry, capability_spec, command_spec, run_capability
from ..capability_policy import Rule, matrix, set_rule
from ..controller import claim_next_operation
from ..security import Capability, Principal, cli_principal
from .procedures import APP, DB, EDGE, RESTART, RESTART_KEY, RESTART_RUN, REVISION, Supply, manifest, run_payload

# A person's own click in the browser, which is their consent.
OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
KIND = "automation.operation"
LOGS = "example.service_logs"


class RunTestCase(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def ask(self, payload, *, principal=OPERATOR, key="example-run-1", name=RESTART_RUN):
        return run_capability(name, payload, principal=principal, idempotency_key=key).answer

    def refused(self, payload, code, **asked):
        answer = self.ask(payload, **asked)
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(answer["error"]["code"], code, answer)
        return answer["error"]


class QueuedRunTests(RunTestCase):
    def test_a_run_queues_what_the_worker_needs(self):
        answer = self.ask(run_payload(APP, correlation="example-investigation-7"))

        self.assertTrue(answer["ok"], answer)
        self.assertTrue(answer["queued"])
        operation = OperationRequest.objects.get()
        self.assertEqual(operation.action, OperationRequest.Action.RUN)
        self.assertEqual(operation.state, OperationRequest.State.QUEUED)
        self.assertEqual((operation.resource.key, operation.resource.kind), (RESTART_KEY, KIND))
        self.assertEqual(
            operation.input,
            {
                "generation": operation.resource.generation,
                "operation": RESTART,
                "revision": REVISION,
                "parameters": {"service": "web"},
                "hosts": [APP],
                "correlation": "example-investigation-7",
            },
        )
        self.assertEqual(answer["operation"]["correlation"], "example-investigation-7")
        self.assertEqual(operation.resource.spec["revision"], REVISION)

    def test_a_run_and_its_cancel_answer_how_each_stands_as_every_ask_does(self):
        from ..asks import Asked

        asked = self.ask(run_payload())
        cancelled = self.ask({"operation_id": asked["operation"]["id"]}, key="example-cancel-1", name=CANCEL)

        self.assertTrue(Asked.of(asked).standing.live, asked)
        ended = Asked.of(cancelled).standing
        self.assertFalse(ended.live, cancelled)
        self.assertTrue(ended.label, cancelled)

    def test_the_claim_names_the_playbook_the_manifest_declares(self):
        self.supply(manifest(playbook="playbooks/linux/service_restart.yml"))
        self.ask(run_payload())

        claimed = claim_next_operation("example-worker", capabilities=((KIND, "run"),))

        self.assertEqual(claimed["resource"]["spec"]["playbook"], "playbooks/linux/service_restart.yml")
        self.assertEqual(claimed["resource"]["spec"]["revision"], REVISION)

    def test_the_procedure_s_page_says_its_playbook(self):
        from hq.domains.control_plane.provider_adapters.automation import OPERATION

        self.ask(run_payload())

        readout = {label: value for label, _, value in OPERATION.readout(ManagedResource.objects.get().spec, {})}
        self.assertEqual(readout["Playbook file"], "playbooks/service_restart.yml")

    def test_hosts_are_resolved_to_the_machine_each_is(self):
        from ..machines import Machine

        self.supply.know(Machine(APP, aliases=("web.example.com",)), Machine(DB))

        self.assertTrue(self.ask(run_payload("WEB.example.com.", APP, DB))["ok"])

        self.assertEqual(OperationRequest.objects.get().input["hosts"], [APP, DB])

    def test_the_worker_claims_it_by_its_kind_and_action(self):
        self.ask(run_payload())

        claimed = claim_next_operation("example-worker", capabilities=((KIND, "run"),))["operation"]

        self.assertEqual(claimed["id"], str(OperationRequest.objects.get().id))

    def test_a_read_procedure_runs_under_a_read_grant(self):
        self.supply(manifest(id=LOGS, effect="read", retry="safe"))

        answer = run_capability(f"automation.{LOGS}.run", run_payload(), principal=MCP_READER).answer

        self.assertTrue(answer["ok"], answer)
        self.assertEqual(OperationRequest.objects.get().input["operation"], LOGS)

    def test_a_new_revision_pins_the_record_anew(self):
        from .procedures import NEXT_REVISION

        self.ask(run_payload())
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        self.supply(manifest(revision=NEXT_REVISION))

        self.assertTrue(self.ask(run_payload(), key="example-run-2")["ok"])

        resource = ManagedResource.objects.get(key=RESTART_KEY)
        self.assertEqual((resource.spec["revision"], resource.generation), (NEXT_REVISION, 2))


class AgentAskedRunTests(RunTestCase):
    """A procedure that changes a machine waits for a person whenever an agent
    asks for it, and no rule an operator sets for that agent lets it past."""

    CHANGES = (RESTART_RUN, "automation.example.wipe.run")

    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id="example.wipe", effect="destructive"), manifest(id=LOGS, effect="read"))
        AgentIdentity.objects.create(
            client_id=AGENT.actor, interfaces=["mcp"], granted=["read", "manage_infrastructure"]
        )
        self.user = operator(username="example-operator")

    def assert_held(self, name):
        answer = self.ask(run_payload(), principal=AGENT, name=name)

        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_change_an_agent_asks_waits_for_a_person(self):
        for name in self.CHANGES:
            with self.subTest(name):
                self.assert_held(name)

    def test_no_agent_rule_can_allow_a_change(self):
        for name in self.CHANGES:
            with self.subTest(name):
                with self.assertRaises(ValueError):
                    set_rule(subject=AGENT.actor, spec=command_spec(name), rule=Rule.ALLOW, user=self.user)
                # A rule stored past that refusal still lifts nothing.
                CapabilityRule.objects.update_or_create(
                    subject=AGENT.actor, capability=name, defaults={"rule": Rule.ALLOW}
                )
                self.assert_held(name)

    def test_the_agents_page_offers_no_allow_for_a_change(self):
        _columns, groups = matrix()
        offered = {
            row.name: {value for cell in row.cells for value, _ in cell.options}
            for group in groups
            for row in group.rows
        }

        for name in self.CHANGES:
            self.assertNotIn(Rule.ALLOW, offered[name], name)
        self.assertIn(Rule.ALLOW, offered[f"automation.{LOGS}.run"])


class PreviewTests(RunTestCase):
    """Any playbook previews through its own run command, in check mode, held
    to every rule its run is held to."""

    def test_a_preview_reaches_the_worker_as_check_mode_and_a_run_never_does(self):
        self.assertTrue(self.ask(run_payload(check=True))["ok"])

        claimed = claim_next_operation("example-worker", capabilities=((KIND, "run"),))
        self.assertIs(claimed["input"]["check"], True)
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        self.assertTrue(self.ask(run_payload(), key="example-run-2")["ok"])
        acted = OperationRequest.objects.get(state=OperationRequest.State.QUEUED)
        self.assertNotIn("check", acted.input)

    def test_a_preview_is_refused_wherever_its_run_is(self):
        self.supply(manifest(hosts=(DB,)))
        self.refused(run_payload(APP, check=True), "precondition")

        self.assertFalse(OperationRequest.objects.exists())

    def test_an_agent_s_preview_of_a_change_waits_for_a_person_and_approving_it_only_previews(self):
        from .test_approval_staleness import decided

        AgentIdentity.objects.create(
            client_id=AGENT.actor, interfaces=["mcp"], granted=["read", "manage_infrastructure"]
        )
        answer = self.ask(run_payload(check=True), principal=AGENT)
        self.assertEqual(answer.get("status"), "awaiting_approval", answer)
        self.assertFalse(OperationRequest.objects.exists())

        decided(ApprovalRequest.objects.get())

        self.assertIs(OperationRequest.objects.get().input["check"], True)


class DoorbellTests(RunTestCase):
    """A queued run rings the automation worker's doorbell once it is stored, and nothing else does."""

    def setUp(self):
        super().setUp()
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(override_settings(SEVERINO_CONTROLLER_DOORBELL=str(directory / "controller-doorbell")))
        self.marker = directory / "automation-doorbell"

    def test_a_queued_run_rings_once_it_commits(self):
        with self.captureOnCommitCallbacks() as committed:
            self.assertTrue(self.ask(run_payload())["queued"])
            self.assertFalse(self.marker.exists(), "The doorbell rang before the run was stored.")

        for callback in committed:
            callback()
        self.assertTrue(self.marker.exists())

    def test_a_refused_run_does_not_ring(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(run_payload())
        self.marker.unlink()

        with self.captureOnCommitCallbacks(execute=True):
            self.refused(run_payload(DB), "precondition", key="example-run-2")

        self.assertFalse(self.marker.exists())

    def test_a_retried_run_does_not_ring_again(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.ask(run_payload())
        self.marker.unlink()

        with self.captureOnCommitCallbacks(execute=True):
            self.ask(run_payload())

        self.assertEqual(OperationRequest.objects.count(), 1)
        self.assertFalse(self.marker.exists())


class RefusedRunTests(RunTestCase):
    def test_an_unknown_procedure_is_no_command(self):
        self.refused(run_payload(), "unknown_capability", name="automation.example.unknown.run")

    def test_a_machine_the_procedure_is_not_written_for_is_refused(self):
        self.supply(manifest(hosts=(DB,)))

        error = self.refused(run_payload(APP), "precondition")

        self.assertEqual(error["details"]["refused"], [APP])
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_kind_the_procedure_is_not_written_for_is_refused(self):
        self.supply(manifest(target_kinds=("example.vm",)))

        self.refused(run_payload(APP), "precondition")

    def test_a_machine_the_worker_has_no_login_to_as_the_run_s_profile_is_refused_when_asked(self):
        self.supply(
            manifest(),
            manifest(id="example.package_upgrade", privilege="example.package_admin"),
            manifest(id=LOGS, effect="read", retry="safe"),
        )
        # The read login to the app, and the change login to the database.
        self.supply.reach((APP, "read"), (DB, "example.package_admin"))

        unprivileged = self.refused(run_payload(APP, DB), "precondition")
        privileged = self.refused(
            run_payload(APP), "precondition", name="automation.example.package_upgrade.run", key="example-run-2"
        )
        self.assertEqual(unprivileged["details"]["missing"], [{"machine": DB, "profile": "read"}])
        self.assertEqual(privileged["details"]["missing"], [{"machine": APP, "profile": "example.package_admin"}])
        self.assertFalse(OperationRequest.objects.exists())

        # A read, and a change that names no privilege, go through the read login.
        self.assertTrue(self.ask(run_payload(APP), name=f"automation.{LOGS}.run", key="example-run-3")["queued"])
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        self.assertTrue(self.ask(run_payload(APP), key="example-run-4")["queued"])

    def test_an_agent_s_change_no_login_reaches_is_refused_before_a_person_is_asked(self):
        from hq.domains.control_plane.models import ApprovalRequest

        self.supply.reach()

        self.refused(run_payload(APP), "precondition", principal=AGENT)

        self.assertFalse(ApprovalRequest.objects.exists())

    def test_a_host_hq_does_not_know_is_refused(self):
        error = self.refused(run_payload("unknown.example.com"), "not_found")

        self.assertEqual(error["details"]["unknown"], ["unknown.example.com"])

    def test_a_procedure_switched_off_is_refused(self):
        self.ask(run_payload())
        OperationRequest.objects.update(state=OperationRequest.State.SUCCEEDED, completed_at="2026-01-01T00:00:00Z")
        ManagedResource.objects.filter(key=RESTART_KEY).update(enabled=False)

        self.refused(run_payload(), "precondition", key="example-run-2")


class OneWriterTests(RunTestCase):
    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id=LOGS))
        self.first = self.ask(run_payload(APP, DB))["operation"]

    def test_a_run_on_a_machine_another_run_holds_is_refused_naming_it(self):
        error = self.refused(run_payload(DB), "precondition", name=f"automation.{LOGS}.run", key="example-run-2")

        self.assertEqual(error["details"]["holding"]["id"], self.first["id"])
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_a_second_run_of_one_procedure_is_refused_naming_the_first(self):
        error = self.refused(run_payload(EDGE), "precondition", key="example-run-2")

        self.assertEqual(error["details"]["holding"]["id"], self.first["id"])

    def test_a_run_on_other_machines_is_queued(self):
        self.assertTrue(self.ask(run_payload(EDGE), name=f"automation.{LOGS}.run", key="example-run-2")["queued"])

    def test_a_machine_is_free_once_its_run_ends(self):
        OperationRequest.objects.update(state=OperationRequest.State.FAILED, completed_at="2026-01-01T00:00:00Z")

        self.assertTrue(self.ask(run_payload(DB), name=f"automation.{LOGS}.run", key="example-run-2")["queued"])

    def test_the_refusal_says_whether_the_playbook_or_a_machine_is_held(self):
        machine = self.refused(run_payload(DB), "precondition", name=f"automation.{LOGS}.run", key="example-run-2")
        playbook = self.refused(run_payload(EDGE), "precondition", key="example-run-3")

        self.assertEqual((machine["details"]["shared"], machine["details"]["machines"]), ([DB], sorted((APP, DB))))
        self.assertEqual(playbook["details"]["shared"], [])


class LapsedHoldTests(RunTestCase):
    """A run whose worker stopped renewing holds neither its playbook nor its
    machines, as the next claim would end it."""

    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id=LOGS))
        self.first = self.ask(run_payload(APP, DB))["operation"]
        claim_next_operation("example-worker", capabilities=((KIND, "run"),))
        OperationRequest.objects.filter(pk=self.first["id"]).update(lease_expires_at="2026-01-01T00:00:00Z")

    def test_its_machine_takes_another_playbook_s_run(self):
        self.assertTrue(self.ask(run_payload(DB), name=f"automation.{LOGS}.run", key="example-run-2")["queued"])

    def test_its_playbook_runs_again(self):
        self.assertTrue(self.ask(run_payload(EDGE), key="example-run-2")["queued"])

    def test_it_ends_as_stopped_early_with_its_outcome_unknown(self):
        self.ask(run_payload(EDGE), key="example-run-2")

        lapsed = OperationRequest.objects.get(pk=self.first["id"])
        self.assertEqual(lapsed.get_state_display(), "Stopped early")
        self.assertEqual(lapsed.result["reason"], "OutcomeUnknown")

    def test_a_live_lease_still_holds(self):
        OperationRequest.objects.filter(pk=self.first["id"]).update(lease_expires_at="2999-01-01T00:00:00Z")

        self.refused(run_payload(DB), "precondition", name=f"automation.{LOGS}.run", key="example-run-2")


class HeldRunPageTests(RunTestCase):
    """The Run page says which run is in the way, in words, and links to it."""

    def setUp(self):
        super().setUp()
        self.supply(manifest(), manifest(id=LOGS))
        self.first = self.ask(run_payload(APP, DB))["operation"]
        operator(self.client)
        machines = patch(
            "hq.platform.application.machines.machine_catalog", side_effect=lambda **_: self.supply.machines
        )
        machines.start()
        self.addCleanup(machines.stop)

    def post(self, name, *hosts):
        from django.urls import reverse

        return self.client.post(
            reverse("command", args=[name]),
            {"hosts": list(hosts), "parameters.service": "web", "__confirm_effect": "on", "__retry_key": name},
        )

    def test_a_held_machine_names_it_and_the_run_on_it_with_a_link(self):
        from django.urls import reverse

        page = self.post(f"automation.{LOGS}.run", DB)

        said = text(page)
        self.assertEqual(page.status_code, 409)
        self.assertNotIn("details.holding", said)
        self.assertIn(f"{DB} is busy with a run of", said)
        self.assertIn("waiting since", said)
        (holder,) = [href for words, href in links(page.content) if words == "See that run"]
        self.assertEqual(holder, reverse("jobs:run", kwargs={"pk": self.first["id"]}))
        self.assertEqual(self.client.get(holder).status_code, 200)

    def test_a_held_playbook_names_its_run_s_machines(self):
        said = text(self.post(RESTART_RUN, EDGE))

        self.assertIn(f"already has a run on {APP} and {DB}, waiting since", said)


class InvalidRunTests(RunTestCase):
    def test_nothing_is_queued_for_any_of_them(self):
        self.refused(run_payload(playbook="site.yml"), "invalid_input")

        self.assertFalse(OperationRequest.objects.exists())
        self.assertFalse(ManagedResource.objects.filter(kind=KIND).exists())


class RetriedRunTests(RunTestCase):
    def test_the_same_key_returns_the_same_run(self):
        first = self.ask(run_payload())
        again = self.ask(run_payload())

        self.assertEqual(again["operation"]["id"], first["operation"]["id"])
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_the_same_key_with_other_parameters_is_a_conflict(self):
        self.ask(run_payload())

        self.refused(run_payload(service="db"), "idempotency_conflict")
        self.assertEqual(OperationRequest.objects.count(), 1)

    def test_the_shell_queues_without_a_key(self):
        answer = run_capability(RESTART_RUN, run_payload(), principal=cli_principal()).answer

        self.assertTrue(answer["queued"], answer)


class CapabilityShapeTests(RunTestCase):
    def test_its_effect_and_grant_are_the_manifest_s(self):
        self.supply(
            manifest(),
            manifest(id=LOGS, effect="read", retry="safe"),
            manifest(id="example.wipe", effect="destructive"),
        )
        effects = {
            spec.name: (spec.effect, spec.required_capabilities)
            for spec in capability_registry()
            if spec.name.startswith("automation.") and spec.name.endswith(".run")
        }

        self.assertEqual(
            effects,
            {
                RESTART_RUN: ("infrastructure_change", (Capability.MANAGE_INFRASTRUCTURE,)),
                f"automation.{LOGS}.run": ("read", (Capability.READ,)),
                "automation.example.wipe.run": ("destructive", (Capability.MANAGE_INFRASTRUCTURE,)),
            },
        )

    def test_a_procedure_no_longer_supplied_is_no_longer_a_command(self):
        self.supply()

        self.assertIsNone(capability_spec(RESTART_RUN))


class FindingInvestigationTests(TestCase):
    """A queue link opens the adopted command with context from a readable finding."""

    MACHINE = "docker-host-01"
    SERVICE = "example-upload.service"
    PLAYBOOK = "example.unit_history"
    COMMAND = f"automation.{PLAYBOOK}.run"

    @classmethod
    def setUpTestData(cls):
        from hq.platform.core import seeds
        from hq.platform.core.example_estate.testing import throwaway_media

        with throwaway_media(), seeds.built("showcase") as owner:
            cls.owner = owner

    def setUp(self):
        from typing import Annotated

        from pydantic import StringConstraints

        from hq.platform.application.contracts import Closed

        from ..machines import Machine

        class Input(Closed):
            service_unit: Annotated[str, StringConstraints(pattern=r"^[a-z.-]+[.]service$")]

        self.input = Input
        self.supply = Supply(
            self, manifest(id=self.PLAYBOOK, effect="read", input_type=Input, investigates=("unit-restarting",))
        )
        self.supply.know(Machine(self.MACHINE))
        self.client.force_login(self.owner)

    def offered(self):
        from django.urls import reverse

        queue = self.client.get(reverse("action_items"))
        return [href for _, href in links(queue.content) if self.COMMAND in href]

    def test_the_queue_opens_the_run_with_the_service_and_queues_what_it_prefilled(self):
        from django.urls import reverse
        from django.utils import timezone

        (url,) = self.offered()
        form = self.client.get(url).context["form"]
        self.assertEqual(form.initial["hosts"], [self.MACHINE])
        self.assertEqual(form.initial["parameters.service_unit"], self.SERVICE)
        self.assertEqual(
            self.client.get(f"{url}&parameters.service_unit=wrong.service").context["form"].initial,
            form.initial,
        )
        self.assertFalse(
            OperationRequest.objects.filter(action=OperationRequest.Action.RUN, input__operation=self.PLAYBOOK).exists()
        )
        OperationRequest.objects.filter(
            action=OperationRequest.Action.RUN,
            state__in=(OperationRequest.State.QUEUED, OperationRequest.State.CLAIMED),
        ).update(state=OperationRequest.State.SUCCEEDED, completed_at=timezone.now())
        response = self.client.post(
            url,
            {"hosts": form.initial["hosts"], "parameters.service_unit": form.initial["parameters.service_unit"]},
        )
        self.assertEqual(response.status_code, 302, text(response))
        queued = OperationRequest.objects.get(action=OperationRequest.Action.RUN, input__operation=self.PLAYBOOK)
        self.assertEqual(queued.input["hosts"], [self.MACHINE])
        self.assertEqual(queued.input["parameters"], {"service_unit": self.SERVICE})
        self.assertEqual(self.client.get(reverse("jobs:run", kwargs={"pk": queued.pk})).status_code, 200)

    def test_a_finding_the_reader_cannot_see_supplies_no_inputs(self):
        from urllib.parse import parse_qs, urlsplit

        from hq.platform.core.testing import NOBODY

        from ..finding_runs import finding_run

        (url,) = self.offered()
        (key,) = parse_qs(urlsplit(url).query)["finding"]
        self.assertIsNone(finding_run(self.COMMAND, key, NOBODY))

    def test_a_worker_that_cannot_reach_the_machine_is_offered_no_run(self):
        self.supply.reach()
        self.assertEqual(self.offered(), [])

    def test_a_fix_whose_run_needs_an_input_of_its_own_is_never_offered_as_it_stands(self):
        self.supply(manifest(id=self.PLAYBOOK, input_type=self.input, fixes=("unit-restarting",)))
        self.assertEqual(self.offered(), [])

    def test_multiple_restarting_services_leave_the_input_open(self):
        from hq.domains.control_plane.models import ProviderInventory

        (url,) = self.offered()
        for inventory in ProviderInventory.objects.filter(kind="host.unit"):
            extra = next((row for row in inventory.records if row.get("unit") == self.SERVICE), None)
            if extra:
                inventory.records = [*inventory.records, {**extra, "unit": "example-second.service"}]
                inventory.save(update_fields=["records"])
                break
        else:
            self.fail("The showcase has no restarting service.")

        form = self.client.get(url).context["form"]
        self.assertEqual(form.initial["hosts"], [self.MACHINE])
        self.assertNotIn("parameters.service_unit", form.initial)

    def test_a_full_filesystem_opens_the_run_on_its_mount(self):
        from typing import Annotated

        from pydantic import StringConstraints

        from hq.platform.application.contracts import Closed

        class Input(Closed):
            mount: Annotated[str, StringConstraints(pattern=r"^/[A-Za-z0-9/_.-]*$")]

        self.supply(
            manifest(id=self.PLAYBOOK, effect="read", input_type=Input, investigates=("filesystem-nearly-full",))
        )

        (url,) = self.offered()
        form = self.client.get(url).context["form"]
        self.assertEqual(form.initial["hosts"], [self.MACHINE])
        self.assertEqual(form.initial["parameters.mount"], "/")

    def test_a_job_on_hqs_machine_is_looked_into_on_that_machine(self):
        """A worker that stopped reporting is about no machine; the one its unit runs on is the one read."""

        from hq.domains.control_plane.models import ProviderInventory

        unit = "severino-hq-events.service"
        inventory = ProviderInventory.objects.get(kind="host.unit")
        inventory.records = [
            *inventory.records,
            {
                "unit": unit,
                "load": "loaded",
                "active": "active",
                "sub": "running",
                "read_at": inventory.records[0]["read_at"],
            },
        ]
        inventory.save(update_fields=["records"])
        self.supply(
            manifest(id=self.PLAYBOOK, effect="read", input_type=self.input, investigates=("event-channel-silent",))
        )

        (url,) = self.offered()
        form = self.client.get(url).context["form"]
        self.assertEqual(form.initial["hosts"], [self.MACHINE])
        self.assertEqual(form.initial["parameters.service_unit"], unit)
