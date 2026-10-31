"""The example estate reads like an HQ that has run for months: every page the
navigation offers has something on it, and its history is spread over that
time by the people and machines that made it."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.contacts import inbox
from hq.domains.content.models import PAGE_TYPES, WRITEUP_TYPES, ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application import analytics, approvals, github_profile
from hq.platform.application.linked_accounts import GITHUB, linked_login
from hq.platform.core import seeds
from hq.platform.core.example_estate.agents import AGENT
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.models import AuditLog


class ExampleEstateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard") as owner:
            cls.user = owner

    def test_every_published_item_has_visits_and_the_site_a_full_quarter(self):
        self.assertTrue(analytics.coverage_summary(days=28)["complete"])
        published = ContentItem.objects.filter(content_type__in=WRITEUP_TYPES | PAGE_TYPES)
        self.assertTrue(published.filter(content_type__in=PAGE_TYPES).exists())
        for item in published:
            with self.subTest(item=item.slug):
                self.assertGreater(analytics.item_traffic(item)["pageviews"], 0)

    def test_the_contact_inbox_is_read_and_holds_mail(self):
        current = inbox.current()

        self.assertEqual(current.state, inbox.OK)
        self.assertTrue(current.rows)

    def test_the_sign_in_names_a_github_account_whose_stars_were_read(self):
        login = linked_login(self.user, GITHUB)

        self.assertTrue(login)
        self.assertTrue(github_profile.profile(login)["watched"])

    def test_the_audit_log_spans_months_by_the_operator_an_agent_and_the_system(self):
        events = AuditLog.objects.all()
        now = timezone.now()

        self.assertLess(min(event.created_at for event in events), now - timedelta(days=180))
        self.assertLessEqual(max(event.created_at for event in events), now)
        self.assertLessEqual({self.user.get_username(), AGENT, "system"}, {event.actor_label for event in events})

    def test_projects_were_not_all_edited_at_once(self):
        edited = {project.updated_at.date() for project in Project.objects.all()}

        self.assertGreater(len(edited), 1)
        self.assertLess(min(edited), timezone.localdate() - timedelta(days=1))

    def test_an_assets_expense_is_dated_and_filed_as_its_purchase(self):
        for asset in Asset.objects.all():
            with self.subTest(asset=asset.slug):
                (expense,) = Expense.objects.filter(related_asset=asset)
                self.assertEqual(expense.date, asset.purchase_date)
                self.assertNotEqual(asset.category, "other")

    def test_some_spending_is_not_all_business(self):
        partial = Expense.objects.exclude(business_use_percentage=100)

        self.assertTrue(partial.exists())

    def test_the_agents_held_request_reads_as_a_before_and_after(self):
        (held,) = approvals.pending()
        preview = approvals.preview(held)

        self.assertEqual(held.requested_actor, AGENT)
        self.assertTrue(preview.compares, "a person reads what is there now beside what would be")
        self.assertTrue(preview.rows)
        self.assertFalse(preview.lines)
