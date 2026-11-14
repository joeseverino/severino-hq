"""A plain record's web form and its command are held to the one model rule, through its capabilities."""

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from django.views.generic import CreateView, DeleteView, UpdateView

from hq.domains.expenses.models import Expense

from ..domains import host_records, load
from ..expenses import ExpenseCommand, save_expense
from ..routes import routed
from ..security import cli_principal
from ..writes import CapabilityDeleteMixin, CapabilityFormMixin, RecordFormMixin, ServiceCreateMixin, WriteMessages


def _views():
    for route in routed():
        view = getattr(route.pattern.callback, "view_class", None)
        if view is not None:
            yield view


def _refuse_vendor(self):
    raise ValidationError({"vendor": "Refused by the model."})


class OneRuleTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user("operator", password="unused-test-pass"))

    def test_a_model_rule_refuses_the_web_and_the_command_alike(self):
        with mock.patch.object(Expense, "clean", _refuse_vendor):
            response = self.client.post(
                reverse("expenses:create"),
                {
                    "date": "2026-07-25",
                    "vendor": "V",
                    "item": "I",
                    "category": "miscellaneous",
                    "total_cost": "1.00",
                    "business_use_percentage": "100",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("Refused by the model.", response.context["form"].errors["vendor"])
            with self.assertRaises(ValidationError) as caught:
                save_expense(
                    ExpenseCommand(date=date(2026, 7, 25), vendor="V", item="I"),
                    principal=cli_principal(),
                )
            self.assertIn("vendor", caught.exception.error_dict)
        self.assertFalse(Expense.objects.exists())


class RecordViewTests(TestCase):
    def test_every_record_form_and_delete_view_writes_through_its_capabilities(self):
        models = {load(records.model): records.resource for records in host_records()}
        wrong, seen = [], set()
        for view in set(_views()):
            resource = models.get(
                getattr(view, "model", None)
                or getattr(getattr(getattr(view, "form_class", None), "_meta", None), "model", None)
            )
            if resource is None:
                continue
            seen.add(resource)
            if issubclass(view, (CreateView, UpdateView)) and not issubclass(view, CapabilityFormMixin):
                wrong.append(view.__qualname__)
            if issubclass(view, DeleteView) and not issubclass(view, CapabilityDeleteMixin):
                wrong.append(view.__qualname__)
        self.assertEqual(wrong, [])
        self.assertEqual(seen, set(models.values()))


class WriteMessageTests(SimpleTestCase):
    def test_a_rewording_mixin_wins_over_the_default_in_either_order(self):
        reworded = type("Reworded", (WriteMessages,), {"created_message": "{noun} {target} added."})
        for write in (ServiceCreateMixin, RecordFormMixin):
            for bases in ((reworded, write), (write, reworded)):
                with self.subTest(bases=[base.__name__ for base in bases]):
                    self.assertEqual(type("AView", bases, {}).created_message, "{noun} {target} added.")
