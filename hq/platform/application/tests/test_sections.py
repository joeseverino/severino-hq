from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

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
        for number in range(sections.PROJECT_ROWS + 1):
            Project.objects.create(name=f"Example {number}", slug=f"example-{number}", status=Project.Status.ACTIVE)
        self.assertEqual(len(sections.recent_active_projects()), sections.PROJECT_ROWS)
