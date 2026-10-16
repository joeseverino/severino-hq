"""Receipts: each names what it is for, and says what went wrong in plain words."""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.domains.receipts.validation import MAX_RECEIPT_BYTES, validate_receipt_file


def _receipt(**fields):
    return Receipt.objects.create(file="receipts/example.pdf", content_type="application/pdf", **fields)


class ReceiptLinkTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("receipt-reader"))
        self.expense = Expense.objects.create(
            date="2030-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("12.00")
        )
        self.asset = Asset.objects.create(item_name="Example Switch", slug="example-switch")

    def test_the_list_names_the_expense_and_the_asset_a_receipt_is_for(self):
        _receipt(vendor="Example Host", amount=Decimal("12.00"), related_expense=self.expense, related_asset=self.asset)
        _receipt(original_filename="scan.pdf")

        response = self.client.get(reverse("receipts:list"))

        self.assertContains(
            response, f'<a href="{self.expense.get_absolute_url()}" data-entity="Expense">Example Host · Hosting</a>'
        )
        self.assertContains(
            response, f'<a href="{self.asset.get_absolute_url()}" data-entity="Asset">Example Switch</a>'
        )
        # A receipt with no vendor is named by its file.
        self.assertContains(response, "<strong>scan.pdf</strong>")
        self.assertContains(response, "No expense or asset")
        self.assertNotContains(response, ">expense</a>")

    def test_a_linked_receipt_says_what_it_is_linked_to(self):
        receipt = _receipt(vendor="Example Host", related_expense=self.expense)

        response = self.client.get(reverse("receipts:match", args=[receipt.pk]))

        self.assertContains(response, "Already linked to")
        self.assertContains(response, f'href="{self.expense.get_absolute_url()}"')

    def test_the_page_says_the_file_type_as_a_word(self):
        receipt = _receipt(vendor="Example Host")

        response = self.client.get(receipt.get_absolute_url())

        self.assertContains(response, "<dt>File type</dt><dd>PDF</dd>", html=True)
        self.assertNotContains(response, "application/pdf")

    def test_uploading_from_an_expense_starts_on_that_expense(self):
        response = self.client.get(reverse("receipts:create"), {"related_expense": self.expense.pk})

        self.assertEqual(response.context["form"].initial["related_expense"], self.expense.pk)

    def test_a_new_expense_from_a_receipt_starts_with_what_the_receipt_says(self):
        response = self.client.get(reverse("expenses:create"), {"vendor": "Example Host", "cost": "12.00"})

        self.assertEqual(response.context["form"].initial["vendor"], "Example Host")
        self.assertEqual(response.context["form"].initial["total_cost"], "12.00")


class ReceiptFileRuleTests(SimpleTestCase):
    def test_a_file_over_the_limit_is_refused_in_megabytes(self):
        upload = SimpleUploadedFile("big.pdf", b"", content_type="application/pdf")
        upload.size = MAX_RECEIPT_BYTES + 1024 * 1024

        with self.assertRaises(ValidationError) as refused:
            validate_receipt_file(upload)

        self.assertEqual(refused.exception.messages, ["This file is 16 MB. The limit is 15 MB."])
