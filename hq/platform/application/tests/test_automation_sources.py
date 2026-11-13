"""A Refresh queues one scan of the automation source for the worker, which
claims it over the bridge and reports what the repository declares; HQ keeps
that as a proposal, judged, and nothing of it becomes runnable.

The catalog the worker reports is the one it records scanning the example
project (``hq_runner/tests/recorded_scan.json``), so the two sides meet on one
fixture.
"""

import copy
import json
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, tag

from hq.domains.control_plane.models import ManagedResource, OperationRequest
from hq.domains.control_plane.tests import bridge_client
from hq.platform.core.audit import audited_labels
from hq.platform.core.models import AuditLog

from ..capabilities import run_capability
from ..security import Capability, Principal

RECORDED = Path(__file__).resolve().parents[4] / "hq_runner" / "tests" / "recorded_scan.json"
REFRESH = "automation.source.refresh"
OPERATOR = Principal("example-operator", "web", frozenset({Capability.READ, Capability.MANAGE_INFRASTRUCTURE}))
READER = Principal("example-reader", "web", frozenset({Capability.READ}))
WORKER = "example-worker"
SCANS = ["automation.source:scan"]
DOORBELL = "hq.platform.application.automation_sources.ring_automation_doorbell"

# A catalog in the contract's shape, small enough to read at a glance.
CATALOG = {
    "branch": "main",
    "revision": "0" * 40,
    "verification": {"verified": True, "reason": "valid", "committer": "web-flow"},
    "procedures": [
        {
            "sidecar": "playbooks/check.hq.yml",
            "playbook": "playbooks/check.yml",
            "declaration": {
                "id": "example.check",
                "target_kinds": ["machine"],
                "effect": "read",
                "timeout_seconds": 60,
                "retry": "safe",
                "recovery": ["Nothing to recover from."],
            },
            "title": "Check a machine",
            "roles": [],
            "preconditions": [],
            "verification": [],
            "problems": [],
        }
    ],
    "files": [{"path": "playbooks/check.yml", "object": "1" * 40}],
    "unpinned": [],
    "reaches": [],
}


def proposals() -> type:
    from hq.domains.control_plane.models import SourceProposal

    return SourceProposal


class RefreshTestCase(TestCase):
    def refresh(self, principal: Principal = OPERATOR, key: str = "example-refresh") -> dict:
        with self.captureOnCommitCallbacks(execute=True), patch(DOORBELL) as self.rung:
            return run_capability(REFRESH, {}, principal=principal, idempotency_key=key).answer

    def claim(self) -> dict:
        return bridge_client.call("claim", controller_id=WORKER, lease_seconds=300, capability=SCANS)

    def report(self, operation_id: str, generation: int, **fields) -> bridge_client.Answer:
        return bridge_client.request(
            "/report",
            query={"controller-id": WORKER, "operation": operation_id},
            body=json.dumps({"observed_generation": generation, **fields}).encode(),
        )

    def scanned(self, catalog: dict, key: str = "example-refresh") -> OperationRequest:
        """A Refresh, its claim, and the worker's report of ``catalog``."""

        self.refresh(key=key)
        claimed = self.claim()
        operation = claimed["operation"]
        answer = self.report(
            operation["id"],
            claimed["resource"]["generation"],
            success=True,
            message="Found the procedures.",
            catalog=catalog,
        )
        self.assertEqual(answer.status, 200, answer.body)
        return OperationRequest.objects.get(pk=operation["id"])


class RefreshTests(RefreshTestCase):
    def test_a_refresh_queues_one_scan_for_the_worker_and_rings_for_it(self):
        answer = self.refresh()

        self.assertTrue(answer.get("queued"), answer)
        operation = OperationRequest.objects.get(pk=answer["operation"]["id"])
        self.assertEqual((operation.resource.kind, operation.action), ("automation.source", "scan"))
        self.rung.assert_called_once()

    def test_a_second_refresh_while_one_waits_is_answered_that_one(self):
        first = self.refresh()
        second = self.refresh(key="example-refresh-again")

        self.assertFalse(second["queued"])
        self.assertEqual(second["operation"]["id"], first["operation"]["id"])
        self.rung.assert_not_called()

    def test_a_refresh_is_refused_to_someone_who_cannot_manage_infrastructure(self):
        answer = self.refresh(READER)

        self.assertEqual(answer["error"]["code"], "forbidden", answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_a_refresh_takes_no_repository_or_anything_else(self):
        answer = run_capability(
            REFRESH, {"repository": "example/other"}, principal=OPERATOR, idempotency_key="example"
        ).answer

        self.assertEqual(answer["error"]["code"], "invalid_input", answer)
        self.assertFalse(OperationRequest.objects.exists())

    def test_only_a_worker_claiming_scans_is_handed_one(self):
        self.refresh()

        runs = bridge_client.call(
            "claim", controller_id=WORKER, lease_seconds=300, capability=["automation.operation:run"]
        )

        self.assertIsNone(runs["operation"])
        self.assertIsNotNone(self.claim()["operation"])


class ProposalTests(RefreshTestCase):
    def test_what_a_scan_reports_is_kept_as_a_proposal_and_nothing_of_it_runs(self):
        operation = self.scanned(CATALOG)

        self.assertEqual(operation.state, OperationRequest.State.SUCCEEDED)
        proposal = proposals().objects.get(operation=operation)
        self.assertEqual(operation.result["proposal"], proposal.pk)
        self.assertEqual((proposal.revision, proposal.verified, proposal.committer), ("0" * 40, True, "web-flow"))
        ((check,),) = [proposal.procedures]
        self.assertEqual(check["problems"], [])
        self.assertFalse(ManagedResource.objects.filter(kind="automation.operation").exists())

    def test_the_logins_the_newest_scan_reports_are_the_ones_a_run_is_held_to(self):
        from ..automation_runs import runner_reaches

        first = {**CATALOG, "reaches": [{"machine": "app.example.com", "profile": "read"}]}
        self.scanned(first)
        self.assertEqual(runner_reaches(), {("app.example.com", "read")})

        later = {**CATALOG, "reaches": [{"machine": "DB.example.com", "profile": "example.service_control"}]}
        self.scanned(later, key="example-refresh-later")
        self.assertEqual(runner_reaches(), {("db.example.com", "example.service_control")})

    def test_the_proposal_is_audited_under_the_source(self):
        operation = self.scanned(CATALOG)

        row = AuditLog.objects.get(object_type=audited_labels()[proposals()], action=AuditLog.Action.CREATED)
        self.assertEqual(row.metadata.get("subject", row.object_repr), operation.resource.key)

    def test_a_sidecar_hq_refuses_carries_why_in_its_problems(self):
        catalog = copy.deepcopy(CATALOG)
        twin = copy.deepcopy(catalog["procedures"][0])
        catalog["procedures"][0]["declaration"]["hosts"] = ["App.Example.com"]
        twin["sidecar"] = "playbooks/twin.hq.yml"
        twin["declaration"]["privilege"] = "example.root"
        catalog["procedures"].append(twin)

        proposal = proposals().objects.get(operation=self.scanned(catalog))

        named, privileged = (" ".join(procedure["problems"]) for procedure in proposal.procedures)
        self.assertIn("hosts", named)
        self.assertIn("privilege", privileged)
        for problems in (named, privileged):
            self.assertIn("example.check", problems)

    def test_a_failed_scan_keeps_no_proposal(self):
        self.refresh()
        claimed = self.claim()

        answer = self.report(
            claimed["operation"]["id"], claimed["resource"]["generation"], success=False, message="No default branch."
        )

        self.assertEqual(answer.status, 200, answer.body)
        self.assertFalse(proposals().objects.exists())

    def test_a_scan_that_succeeded_without_a_catalog_is_refused(self):
        self.refresh()
        claimed = self.claim()

        answer = self.report(claimed["operation"]["id"], claimed["resource"]["generation"], success=True)

        self.assertNotEqual(answer.status, 200)
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CLAIMED)

    def test_a_catalog_over_the_contract_is_refused_at_the_bridge(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["procedures"][0]["title"] = "x" * 201
        self.refresh()
        claimed = self.claim()

        answer = self.report(
            claimed["operation"]["id"], claimed["resource"]["generation"], success=True, catalog=catalog
        )

        self.assertEqual(answer.status, 422, answer.body)
        self.assertFalse(proposals().objects.exists())

    def test_a_catalog_from_work_other_than_a_scan_is_refused(self):
        from ..controller import ControllerReport, report_operation

        self.refresh()
        operation = self.claim()["operation"]
        OperationRequest.objects.filter(pk=operation["id"]).update(action=OperationRequest.Action.RECONCILE)

        with self.assertRaisesMessage(ValueError, "Only a scan"):
            report_operation(
                operation["id"],
                ControllerReport(success=True, observed_generation=1, catalog=CATALOG),
                controller_id=WORKER,
            )


@tag("source")
class ExampleProjectTests(RefreshTestCase):
    def test_a_refresh_stores_the_example_project_s_procedures_as_hq_judged_them(self):
        recorded = json.loads(RECORDED.read_text(encoding="utf-8"))

        proposal = proposals().objects.get(operation=self.scanned(recorded))

        self.assertEqual((proposal.branch, proposal.revision), (recorded["branch"], recorded["revision"]))
        by_sidecar = {procedure["sidecar"]: procedure for procedure in proposal.procedures}
        self.assertEqual(by_sidecar["playbooks/host_health.hq.yml"]["problems"], [])
        self.assertEqual(by_sidecar["playbooks/service_restart.hq.yml"]["problems"], [])
        unfinished = " ".join(by_sidecar["playbooks/unfinished.hq.yml"]["problems"])
        for missing in ("playbook", "id", "effect"):
            self.assertIn(missing, unfinished)
        self.assertEqual(
            {(found["path"], found["kind"]) for found in proposal.unpinned},
            {
                ("ansible.cfg", "config"),
                ("callback_plugins", "plugins"),
                ("collections", "plugins"),
                ("roles/service/library", "plugins"),
                ("group_vars", "variables"),
            },
        )
        self.assertEqual(proposal.files, recorded["files"])
