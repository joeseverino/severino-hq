"""The demo seed links what it plans and is safe to run again."""

from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project


def seed() -> None:
    call_command("seed_demo", stdout=StringIO())


class SeedDemoTests(TestCase):
    def test_planned_links_are_made(self):
        seed()

        writeup = ContentItem.objects.get(slug="homelab-dns-writeup")
        self.assertEqual(list(writeup.related_projects.values_list("slug", flat=True)), ["homelab-dns"])
        self.assertEqual(sorted(writeup.related_assets.values_list("slug", flat=True)), ["mini-pc", "unifi-flex-mini"])
        runbook = DocumentationRecord.objects.get(doc_id="rb-adguard-001")
        self.assertEqual(list(runbook.related_assets.values_list("slug", flat=True)), ["mini-pc"])

    def test_running_it_again_creates_nothing(self):
        seed()
        counts = [model.objects.count() for model in (Project, ContentItem, DocumentationRecord, Expense)]

        seed()

        self.assertEqual(
            [model.objects.count() for model in (Project, ContentItem, DocumentationRecord, Expense)], counts
        )
