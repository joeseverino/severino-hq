from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from hq.domains.content.models import ContentItem
from hq.domains.projects.models import Project
from hq.platform.application import sections


class ExpensesCardTests(TestCase):
    def card(self, total, deductible):
        reading = {"count": 3, "total": Decimal(total), "deductible": Decimal(deductible), "year": 2026}
        with (
            patch.object(sections, "expenses_reading", return_value=reading),
            patch.object(sections, "reverse", return_value="/expenses/"),
        ):
            cards = sections.expenses()
        self.assertEqual(len(cards), 1)
        return cards[0]

    def test_a_deductible_share_is_named_by_its_amount(self):
        self.assertEqual(self.card("100.00", "40.00")["detail"], f"{sections.money(Decimal('40.00'))} deductible est.")

    def test_a_wholly_deductible_year_does_not_repeat_its_total(self):
        card = self.card("100.00", "100.00")
        self.assertEqual(card["detail"], "All deductible est.")
        self.assertNotIn(card["value"], card["detail"])


class ProjectRowsTests(TestCase):
    def test_the_dashboard_lists_as_many_projects_as_the_month_has_weeks(self):
        for number in range(sections.CALENDAR_ROWS + 1):
            Project.objects.create(name=f"Example {number}", slug=f"example-{number}", status=Project.Status.ACTIVE)
        self.assertEqual(len(sections.recent_active_projects()), sections.CALENDAR_ROWS)

    def test_publishing_lists_as_many_as_the_projects_beside_it(self):
        for number in range(sections.CALENDAR_ROWS + 1):
            ContentItem.objects.create(
                title=f"Example {number}", slug=f"example-{number}", status=ContentItem.Status.PUBLISHED
            )
        self.assertEqual(len(sections.recently_published()), sections.CALENDAR_ROWS)


class UpdatesCardTests(TestCase):
    def cards(self):
        return {card["id"]: card for card in sections.projects()}

    def test_the_card_counts_open_updates_across_opted_in_projects_with_unanswered_alerts_beside(self):
        from .test_github_estate import ANSWERED, BUMP, UNANSWERED, UPDATE, store

        store(pull_requests=[UPDATE, BUMP], dependabot_alerts=[ANSWERED, UNANSWERED])
        Project.objects.create(
            name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha", dependency_updates=True
        )
        # Opted in but not read: counted as a project, adding nothing.
        Project.objects.create(
            name="Beta", slug="beta", repository_url="https://github.com/example/beta", dependency_updates=True
        )

        card = self.cards()["hq.projects.updates"]

        self.assertEqual(card["value"], "1")
        self.assertTrue(card["detail"].startswith("1 security alert"), card["detail"])
        self.assertTrue(card["url"].endswith("?updates_on=1"))

    def test_how_many_projects_are_active_is_left_to_the_list_of_them(self):
        Project.objects.create(name="Alpha", slug="alpha", status=Project.Status.ACTIVE)

        self.assertEqual(self.cards(), {})

    def test_without_an_opted_in_project_there_is_no_card(self):
        Project.objects.create(name="Alpha", slug="alpha", repository_url="https://github.com/example/alpha")

        self.assertNotIn("hq.projects.updates", self.cards())
