"""Each domain's application service, reached the same way from every adapter."""

from datetime import date

from django.test import TestCase

from hq.domains.expenses.models import Expense
from hq.domains.projects.github import GitHubMetadataError, fetch_last_push
from hq.domains.projects.models import Project

from ..documentation import sync_documentation
from ..expenses import ExpenseCommand, save_expense
from ..record_errors import RecordNotFound
from ..security import OPERATOR_CAPABILITIES, Principal, cli_principal


class ProjectApplicationServiceTests(TestCase):
    def test_github_gateway_rejects_non_repository_urls_before_network_access(self):
        with self.assertRaises(GitHubMetadataError):
            fetch_last_push("https://example.com/example/hq")


class DocumentationSyncTests(TestCase):
    manifest = [
        {
            "doc_id": "rb-shared-service",
            "title": "Shared service",
            "doc_type": "runbook",
            "status": "active",
            "system": "Example service",
            "environment": "other",
            "sensitivity": "internal",
        }
    ]

    def test_sync_is_idempotent_and_prune_fails_closed(self):
        agent = Principal("test-agent", "mcp", OPERATOR_CAPABILITIES)
        first = sync_documentation(self.manifest, principal=agent)
        second = sync_documentation(self.manifest, principal=agent)

        self.assertTrue(first["ok"])
        self.assertEqual(first["stats"]["created"], 1)
        self.assertEqual(second["stats"]["skipped"], 1)
        with self.assertRaises(ValueError):
            sync_documentation([], principal=agent, prune_orphans=True)


class ExpenseApplicationServiceTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Ops", slug="ops")

    def test_relationship_failure_rolls_back(self):
        with self.assertRaises(RecordNotFound):
            save_expense(
                ExpenseCommand(
                    date=date(2026, 7, 25),
                    vendor="Vendor",
                    item="Invalid",
                    related_project="missing",
                ),
                principal=cli_principal(),
            )
        self.assertEqual(Expense.objects.count(), 0)
