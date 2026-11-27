"""A push to the automation source's repository makes its scan due, and HQ
asks for one itself; the worker claims it over the bridge and reports what
the repository declares; HQ keeps that as a proposal, judged, and nothing of
it becomes runnable.

The catalog the worker reports is the one it records scanning the example
project (``hq_runner/tests/recorded_scan.json``), so the two sides meet on one
fixture.
"""

import copy
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import call, patch

from django.test import TestCase, tag

from hq.domains.control_plane.models import ManagedResource, OperationRequest, ReadRequest
from hq.domains.control_plane.observations.github import REPOSITORY_KIND
from hq.domains.control_plane.tests import bridge_client
from hq.platform.core.audit import audited_labels
from hq.platform.core.models import AuditLog

from ..report_testing import bridged

RECORDED = Path(__file__).resolve().parents[4] / "hq_runner" / "tests" / "recorded_scan.json"
WORKER = "example-worker"
SCANS = ["automation.source:scan"]
# The file touch every doorbell rings through, which says which doorbell.
DOORBELL = "hq.platform.application.cadence._ring"
# The repository the host's configuration names as the source, as the
# worker reports it (``hq_runner/tests/remote.py``).
SOURCE = "example/automation"

# A catalog in the contract's shape, small enough to read at a glance.
CATALOG = {
    "repository": SOURCE,
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

# As the Worker forwards GitHub's push delivery: the X-GitHub-Event header,
# the payload's repository.full_name and the X-GitHub-Delivery GUID GitHub's
# docs show.
PUSH = {
    "provider": "github_app",
    "event": "push",
    "subject": SOURCE,
    "delivery_id": "72d3162e-cc78-11e3-81ab-4c9367dc0958",
    "received_at": "2026-01-15T12:00:00Z",
}


def proposals() -> type:
    from hq.domains.control_plane.models import SourceProposal

    return SourceProposal


def scans() -> list[OperationRequest]:
    return list(OperationRequest.objects.filter(action=OperationRequest.Action.SCAN).order_by("created_at"))


class ScanTestCase(TestCase):
    def setUp(self):
        # The worker arrives and finds nothing, so a scan HQ asks for is taken.
        self.claim()

    def read(self, head: str, repository: str = SOURCE) -> None:
        """The controller's reading of the repositories the GitHub App reads:
        ``repository`` with its default branch at ``head``."""

        record = {
            "connection_ref": "example-github",
            "repository": repository,
            "url": f"https://github.com/{repository}",
            "head": {"sha": head},
        }
        with patch(DOORBELL) as self.rung, self.captureOnCommitCallbacks(execute=True):
            bridged("inventory", {REPOSITORY_KIND: {"ok": True, "records": [record]}}, controller_id="controller")

    def claim(self) -> dict:
        return bridge_client.call(
            "claim", app=bridge_client.worker(), controller_id=WORKER, lease_seconds=300, capability=SCANS
        )

    def report(self, operation_id: str, generation: int, **fields) -> bridge_client.Answer:
        return bridge_client.request(
            "/report",
            query={"controller-id": WORKER, "operation": operation_id},
            body=json.dumps({"observed_generation": generation, **fields}).encode(),
            app=bridge_client.worker(),
        )

    def scanned(self, catalog: dict) -> OperationRequest:
        """The reading of the source at ``catalog``'s commit, the scan HQ asks
        for, its claim, and the worker's report of ``catalog``."""

        self.read(catalog["revision"])
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


class ScanDueTests(ScanTestCase):
    def test_a_push_to_the_source_makes_its_scan_due_and_hq_asks_for_it_itself(self):
        from ..inbound import apply_events

        self.scanned(CATALOG)
        with patch(DOORBELL, return_value=True), self.captureOnCommitCallbacks(execute=True):
            apply_events({"source": "worker", "events": [PUSH]})
        self.assertIn(REPOSITORY_KIND, ReadRequest.objects.values_list("kind", flat=True))

        self.read("1" * 40)

        _, scan = scans()
        self.assertEqual(scan.state, OperationRequest.State.QUEUED)
        self.assertEqual((scan.requested_interface, scan.requested_actor), ("internal", "controller"))
        asked = AuditLog.objects.get(object_id=str(scan.pk), action=AuditLog.Action.CREATED)
        self.assertEqual(asked.metadata["operation"], "automation.source.scan")
        self.assertEqual(self.rung.call_args_list.count(call("automation")), 1)
        self.assertIsNotNone(self.claim()["operation"])

    def test_a_reading_of_the_commit_scanned_or_of_another_repository_asks_for_none(self):
        self.scanned(CATALOG)

        self.read(CATALOG["revision"])
        self.read("1" * 40, repository="example/other")

        self.assertEqual(len(scans()), 1)
        self.assertNotIn(call("automation"), self.rung.call_args_list)

    def test_a_head_is_asked_for_once_while_its_scan_waits_and_once_a_day_after_it_failed(self):
        self.scanned(CATALOG)
        self.read("1" * 40)
        self.read("1" * 40)
        self.assertEqual(len(scans()), 2)
        claimed = self.claim()
        self.report(claimed["operation"]["id"], claimed["resource"]["generation"], success=False, message="Unread.")

        self.read("1" * 40)
        self.assertEqual(len(scans()), 2)

        with patch("django.utils.timezone.now", return_value=scans()[-1].created_at + timedelta(days=1)):
            # The worker still arrives each pass, so what is asked of it is taken.
            self.claim()
            self.read("1" * 40)
        self.assertEqual(len(scans()), 3)

    def test_before_any_scan_result_the_first_reading_asks_for_one(self):
        self.read("1" * 40, repository="example/other")

        (scan,) = scans()
        self.assertEqual((scan.resource.kind, scan.resource.spec), ("automation.source", {"named_by": "host"}))

    def test_while_no_worker_would_take_it_none_is_asked(self):
        from hq.platform.core.models import UpstreamReading

        from ..cadence import automation_heartbeat

        UpstreamReading.objects.filter(key=automation_heartbeat("read")).delete()

        self.read("1" * 40)

        self.assertEqual(scans(), [])

    def test_only_a_worker_claiming_scans_is_handed_one(self):
        self.read("1" * 40)

        runs = bridge_client.call(
            "claim",
            app=bridge_client.worker(),
            controller_id=WORKER,
            lease_seconds=300,
            capability=["automation.operation:run"],
        )

        self.assertIsNone(runs["operation"])
        self.assertIsNotNone(self.claim()["operation"])


class ProposalTests(ScanTestCase):
    def test_what_a_scan_reports_is_kept_as_a_proposal_and_nothing_of_it_runs(self):
        operation = self.scanned(CATALOG)

        self.assertEqual(operation.state, OperationRequest.State.SUCCEEDED)
        proposal = proposals().objects.get(operation=operation)
        self.assertEqual(operation.result["proposal"], proposal.pk)
        self.assertEqual(
            (proposal.repository, proposal.revision, proposal.verified, proposal.committer),
            (SOURCE, "0" * 40, True, "web-flow"),
        )
        ((check,),) = [proposal.procedures]
        self.assertEqual(check["problems"], [])
        self.assertFalse(ManagedResource.objects.filter(kind="automation.operation").exists())

    def test_the_logins_the_newest_scan_reports_are_the_ones_a_run_is_held_to(self):
        from ..automation_runs import runner_reaches

        first = {**CATALOG, "reaches": [{"machine": "app.example.com", "profile": "read"}]}
        self.scanned(first)
        self.assertEqual(runner_reaches(), {("app.example.com", "read")})

        later = {
            **CATALOG,
            "revision": "1" * 40,
            "reaches": [{"machine": "DB.example.com", "profile": "example.service_control"}],
        }
        self.scanned(later)
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
        self.read("1" * 40)
        claimed = self.claim()

        answer = self.report(
            claimed["operation"]["id"], claimed["resource"]["generation"], success=False, message="No default branch."
        )

        self.assertEqual(answer.status, 200, answer.body)
        self.assertFalse(proposals().objects.exists())

    def test_a_scan_that_succeeded_without_a_catalog_is_refused(self):
        self.read("1" * 40)
        claimed = self.claim()

        answer = self.report(claimed["operation"]["id"], claimed["resource"]["generation"], success=True)

        self.assertNotEqual(answer.status, 200)
        self.assertEqual(OperationRequest.objects.get().state, OperationRequest.State.CLAIMED)

    def test_a_catalog_over_the_contract_is_refused_at_the_bridge(self):
        catalog = copy.deepcopy(CATALOG)
        catalog["procedures"][0]["title"] = "x" * 201
        self.read("1" * 40)
        claimed = self.claim()

        answer = self.report(
            claimed["operation"]["id"], claimed["resource"]["generation"], success=True, catalog=catalog
        )

        self.assertEqual(answer.status, 422, answer.body)
        self.assertFalse(proposals().objects.exists())

    def test_a_catalog_from_work_other_than_a_scan_is_refused(self):
        from ..controller import ControllerReport, report_operation

        self.read("1" * 40)
        operation = self.claim()["operation"]
        OperationRequest.objects.filter(pk=operation["id"]).update(action=OperationRequest.Action.RECONCILE)

        with self.assertRaisesMessage(ValueError, "Only a scan"):
            report_operation(
                operation["id"],
                ControllerReport(success=True, observed_generation=1, catalog=CATALOG),
                controller_id=WORKER,
            )


@tag("source")
class ExampleProjectTests(ScanTestCase):
    def test_a_scan_stores_the_example_project_s_procedures_as_hq_judged_them(self):
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
