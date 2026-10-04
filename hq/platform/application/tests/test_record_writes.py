"""A plain record's web form and its command are held to the one model rule."""

from __future__ import annotations

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.forms import modelform_factory
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.views.generic import CreateView, DeleteView, UpdateView

from hq.domains.expenses.models import Expense

from ..domains import host_records, load
from ..expenses import ExpenseCommand, save_expense
from ..records import save_form
from ..security import AuthorizationError, Principal, cli_principal
from ..writes import RecordDeleteMixin, RecordFormMixin

# Receipts carry a private file: the web upload validates and stores the bytes,
# which a ModelForm save does not, so it keeps its service.
SERVICE_WRITTEN = {"receipts"}


def _views(patterns=None):
    for pattern in get_resolver().url_patterns if patterns is None else patterns:
        if isinstance(pattern, URLResolver):
            yield from _views(pattern.url_patterns)
        elif isinstance(pattern, URLPattern):
            view = getattr(pattern.callback, "view_class", None)
            if view is not None:
                yield view


def _refuse_vendor(self):
    raise ValidationError({"vendor": "Refused by the model."})


class OneRuleTests(TestCase):
    def setUp(self):
        self.client.force_login(
            get_user_model().objects.create_user("operator", password="unused-test-pass")
        )

    def test_a_model_rule_refuses_the_web_and_the_command_alike(self):
        with mock.patch.object(Expense, "clean", _refuse_vendor):
            response = self.client.post(
                reverse("expenses:create"),
                {"date": "2026-07-25", "vendor": "V", "item": "I",
                 "category": "miscellaneous", "total_cost": "1.00",
                 "business_use_percentage": "100"},
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

    def test_the_web_write_validates_fields_the_form_does_not_show(self):
        form = modelform_factory(Expense, fields=("date", "vendor", "item"))(
            {"date": "2026-07-25", "vendor": "V", "item": "I"},
            instance=Expense(category="not-a-category"),
        )
        self.assertTrue(form.is_valid())
        with self.assertRaises(ValidationError) as caught:
            save_form(form, principal=cli_principal())
        self.assertIn("category", caught.exception.error_dict)
        self.assertFalse(Expense.objects.exists())

    def test_the_web_write_needs_the_declared_permission(self):
        form = modelform_factory(Expense, fields=("date", "vendor", "item"))(
            {"date": "2026-07-25", "vendor": "V", "item": "I"}
        )
        self.assertTrue(form.is_valid())
        with self.assertRaises(AuthorizationError):
            save_form(form, principal=Principal("nobody", "web", frozenset()))
        self.assertFalse(Expense.objects.exists())


class RecordViewTests(TestCase):
    def test_every_record_form_and_delete_view_writes_through_the_record_path(self):
        models = {
            load(records.model): records.resource
            for records in host_records()
            if records.resource not in SERVICE_WRITTEN
        }
        wrong, seen = [], set()
        for view in set(_views()):
            resource = models.get(getattr(view, "model", None) or getattr(
                getattr(getattr(view, "form_class", None), "_meta", None), "model", None
            ))
            if resource is None:
                continue
            seen.add(resource)
            if issubclass(view, (CreateView, UpdateView)) and not issubclass(view, RecordFormMixin):
                wrong.append(view.__qualname__)
            if issubclass(view, DeleteView) and not issubclass(view, RecordDeleteMixin):
                wrong.append(view.__qualname__)
        self.assertEqual(wrong, [])
        self.assertEqual(seen, set(models.values()))
