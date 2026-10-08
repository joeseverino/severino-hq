"""Reports: every count opens the list it counts."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project


class ReportsPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("report-reader"))

    def test_status_counts_are_named_and_linked(self):
        Project.objects.create(name="Example Tool", slug="example-tool", status=Project.Status.ACTIVE)
        ContentItem.objects.create(title="Example writeup", slug="example-writeup")

        response = self.client.get(reverse("reports:dashboard"))

        self.assertContains(response, f'<a href="{reverse("projects:list")}?status=active">Active 1</a>')
        self.assertContains(response, f'<a href="{reverse("content:list")}?status=draft">Draft 1</a>')
        self.assertContains(response, "<strong>Writeups and pages</strong>")
        self.assertNotContains(response, "Content · draft")

    def test_categories_are_named_as_the_forms_name_them(self):
        Expense.objects.create(
            date="2030-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("12.00"),
            category="content_production",
        )
        Asset.objects.create(item_name="Example Switch", slug="example-switch", category="networking")

        response = self.client.get(reverse("reports:dashboard"), {"year": 2030})

        self.assertContains(
            response,
            f'<a href="{reverse("expenses:list")}?category=content_production&amp;year=2030">Content production</a>',
        )
        self.assertContains(response, f'<a href="{reverse("assets:list")}?category=networking">Networking gear</a>')
        self.assertContains(response, "1 expense</a>")

    def test_the_page_is_about_money_and_documents_only(self):
        response = self.client.get(reverse("reports:dashboard"))

        self.assertNotContains(response, "Recent audit events")
        self.assertContains(response, "Year summary")
        self.assertNotContains(response, ".csv<")
        self.assertNotIn("recent_audit", response.context)
