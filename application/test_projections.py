from __future__ import annotations

import json
from unittest.mock import patch

from django.test import TestCase

from control_plane.models import ManagedResource, OperationRequest
from projects.models import Project

from .dashboard import operating_snapshot
from .infrastructure import get_managed_resource, operation_summary


class DashboardProjectionTests(TestCase):
    def test_snapshot_is_json_safe_and_owns_priority_counts(self):
        Project.objects.create(
            name="Needs output",
            slug="needs-output",
            status=Project.Status.ACTIVE,
        )

        with patch("application.dashboard.get_unread_count", return_value=2):
            snapshot = operating_snapshot()

        json.dumps(snapshot)
        items = {item["code"]: item for item in snapshot["priority"]}
        self.assertEqual(items["projects_output"]["count"], 1)
        self.assertEqual(items["unread_contacts"]["count"], 2)
        self.assertEqual(
            snapshot["priority_count"],
            sum(item["count"] for item in snapshot["priority"]),
        )


class OperationProjectionTests(TestCase):
    def test_failed_operation_separates_guidance_and_affected_evidence(self):
        resource = ManagedResource.objects.create(
            key="certificate",
            kind="tls.certificate",
            spec={},
        )
        operation = OperationRequest.objects.create(
            resource=resource,
            action=OperationRequest.Action.RECONCILE,
            state=OperationRequest.State.FAILED,
            requested_actor="homelab-controller",
            requested_interface="controller",
            idempotency_key="failed-projection",
            result={
                "message": "Verification did not converge.",
                "conditions": [
                    {
                        "type": "Degraded",
                        "status": True,
                        "reason": "VerificationFailed",
                        "message": "One consumer serves the previous certificate.",
                    }
                ],
                "status": {
                    "expected_fingerprint_sha256": "expected",
                    "consumers": [
                        {
                            "consumer": "npm",
                            "domain": "hq.example.com",
                            "fingerprint_sha256": "observed",
                            "matches_expected": False,
                        }
                    ],
                },
            },
        )

        result = operation_summary(operation)

        self.assertEqual(
            result["headline"], "One consumer serves the previous certificate."
        )
        self.assertEqual(result["condition"]["reason"], "VerificationFailed")
        self.assertEqual(result["affected"][0]["domain"], "hq.example.com")
        self.assertTrue(result["automatic"])
        json.dumps(get_managed_resource("certificate"))
