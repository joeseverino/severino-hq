"""Reports: every count opens the list it counts."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.tests.markup import links, text


class ReportsPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("report-reader"))

    def test_status_counts_are_named_and_linked(self):
        Project.objects.create(name="Example Tool", slug="example-tool", status=Project.Status.ACTIVE)
        ContentItem.objects.create(title="Example writeup", slug="example-writeup")

        response = self.client.get(reverse("reports:dashboard"))

        found = links(response)
        self.assertIn(("Active 1", f"{reverse('projects:list')}?status=active"), found)
        self.assertIn(("Draft 1", f"{reverse('content:list')}?status=draft"), found)
        self.assertIn("Writeups and pages", text(response))
        self.assertNotContains(response, "Content · draft")

    def test_categories_are_named_as_the_forms_name_them(self):
        Expense.objects.create(
            date="2030-01-02",
            vendor="Example Host",
            item="Hosting",
            total_cost=Decimal("12.00"),
            category="content_production",
        )
        Asset.objects.create(item_name="Example Switch", slug="example-switch", category="networking")

        response = self.client.get(reverse("reports:dashboard"), {"year": 2030})

        found = links(response)
        self.assertIn(
            ("Content production", f"{reverse('expenses:list')}?category=content_production&year=2030"), found
        )
        self.assertIn(("Networking gear", f"{reverse('assets:list')}?category=networking"), found)
        self.assertIn("1 expense", [label for label, _href in found])

    def test_the_page_is_about_money_and_documents_only(self):
        response = self.client.get(reverse("reports:dashboard"))

        self.assertNotContains(response, "Recent audit events")
        self.assertContains(response, "Year summary")
        self.assertNotContains(response, ".csv<")
        self.assertNotIn("recent_audit", response.context)
