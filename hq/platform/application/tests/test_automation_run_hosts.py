"""A run's machines are rows written with it, so a machine's runs are one
indexed query. Every manifest here is a synthetic ``example.*`` one."""

from django.test import TestCase

from hq.domains.control_plane.models import OperationHost, OperationRequest

from ..capabilities import run_capability
from ..machines import Machine
from ..security import Capability, Principal
from .procedures import APP, DB, RESTART_RUN, Supply, manifest, run_payload

OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "mcp", frozenset({Capability.READ}))


class RunHostTests(TestCase):
    def setUp(self):
        self.supply = Supply(self, manifest())

    def ask(self, payload, *, principal=OPERATOR, key="example-run-1"):
        return run_capability(RESTART_RUN, payload, principal=principal, idempotency_key=key).answer

    def hosts(self):
        return sorted(OperationHost.objects.values_list("operation_id", "host"))

    def test_each_machine_a_run_names_is_a_row_of_that_run(self):
        answer = self.ask(run_payload(DB, APP))

        self.assertTrue(answer["ok"], answer)
        run = OperationRequest.objects.get()
        self.assertEqual(self.hosts(), [(run.pk, APP), (run.pk, DB)])

    def test_a_machine_named_by_an_alias_is_its_canonical_row(self):
        self.supply.know(Machine(APP, aliases=("web.example.com",)), Machine(DB))

        self.assertTrue(self.ask(run_payload("WEB.example.com.", APP))["ok"])

        self.assertEqual([host for _run, host in self.hosts()], [APP])

    def test_a_retried_run_writes_no_second_set(self):
        self.ask(run_payload(APP))
        self.ask(run_payload(APP))

        self.assertEqual(len(self.hosts()), 1)

    def test_a_refused_run_writes_no_rows(self):
        cases = {
            "denied": {"payload": run_payload(APP), "principal": READER},
            "invalid": {"payload": run_payload(APP, playbook="site.yml")},
            "unknown machine": {"payload": run_payload("unknown.example.com")},
        }
        for case, asked in cases.items():
            with self.subTest(case=case):
                answer = self.ask(asked["payload"], principal=asked.get("principal", OPERATOR))

                self.assertFalse(answer["ok"], answer)
                self.assertEqual(self.hosts(), [])

    def test_a_run_held_off_by_another_on_its_machine_writes_no_rows(self):
        self.supply(manifest(), manifest(id="example.service_logs"))
        first = self.ask(run_payload(APP, DB))["operation"]["id"]

        answer = run_capability(
            "automation.example.service_logs.run", run_payload(DB), principal=OPERATOR, idempotency_key="example-run-2"
        ).answer

        self.assertEqual(answer["error"]["details"]["holding"]["id"], first)
        self.assertEqual({str(run) for run, _host in self.hosts()}, {first})
