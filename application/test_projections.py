from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.template.loader import render_to_string
from django.utils import timezone

from control_plane.models import ManagedResource, OperationRequest
from expenses.models import Expense
from projects.models import Project

from .dashboard import operating_snapshot
from .infrastructure import get_managed_resource, operation_summary
from .ui import ChartSeries, stacked_bar_chart


class UiProjectionTests(TestCase):
    def test_stacked_chart_projects_aligned_series_once(self):
        chart = stacked_bar_chart(
            "Training",
            "Weekly minutes",
            ("Aug 3", "Aug 10"),
            (
                ChartSeries("Run", (30.0, 45.0), 1),
                ChartSeries("Strength", (60.0, 30.0), 2),
            ),
            unit="minutes",
        )

        self.assertFalse(chart.empty)
        self.assertEqual(chart.rows[1].values, (45.0, 30.0))
        self.assertEqual(len(chart.bars), 4)
        rendered = render_to_string(
            "partials/_stacked_bar_chart.html", {"chart": chart}
        )
        self.assertIn("Training", rendered)
        self.assertIn("Run: 45.0 minutes", rendered)
        self.assertIn("View chart data", rendered)

    def test_stacked_chart_rejects_misaligned_series(self):
        with self.assertRaises(ValueError):
            stacked_bar_chart(
                "Training",
                "Weekly minutes",
                ("Aug 3",),
                (ChartSeries("Run", (30.0, 45.0), 1),),
                unit="minutes",
            )


class DashboardProjectionTests(TestCase):
    def test_snapshot_stays_within_its_query_budget(self):
        Project.objects.create(
            name="Query budget",
            slug="query-budget",
            status=Project.Status.ACTIVE,
        )

        with (
            patch("application.dashboard.get_unread_count", return_value=0),
            CaptureQueriesContext(connection) as queries,
        ):
            operating_snapshot()

        self.assertLessEqual(
            len(queries),
            20,
            "Dashboard query budget exceeded:\n"
            + "\n".join(query["sql"] for query in queries),
        )

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

    def test_expense_kpis_respect_fiscal_year_start(self):
        today = timezone.localdate()
        Expense.objects.create(
            date=today,
            vendor="Now",
            item="This fiscal year",
            category="hosting",
            total_cost=Decimal("10.00"),
        )
        Expense.objects.create(
            date=today - timedelta(days=45),
            vendor="Then",
            item="Before the fiscal year started",
            category="hosting",
            total_cost=Decimal("7.00"),
        )
        Expense.objects.create(
            date=today + timedelta(days=1),
            vendor="Future",
            item="Not year-to-date yet",
            category="hosting",
            total_cost=Decimal("99.00"),
        )

        # Fiscal year starting this month: the 45-day-old expense falls outside.
        with (
            override_settings(SEVERINO_FISCAL_YEAR_START_MONTH=today.month),
            patch("application.dashboard.get_unread_count", return_value=0),
        ):
            snapshot = operating_snapshot()
        self.assertEqual(snapshot["kpis"]["expenses_count"], 1)
        self.assertEqual(Decimal(snapshot["kpis"]["expenses_total"]), Decimal("10.00"))

        # Fiscal year starting next month began ~11 months ago: both past
        # expenses fall inside, while the future expense remains excluded.
        with (
            override_settings(SEVERINO_FISCAL_YEAR_START_MONTH=today.month % 12 + 1),
            patch("application.dashboard.get_unread_count", return_value=0),
        ):
            snapshot = operating_snapshot()
        self.assertEqual(snapshot["kpis"]["expenses_count"], 2)


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
