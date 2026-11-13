"""Receipts: each names what it is for, and says what went wrong in plain words."""

import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.domains.assets.models import Asset
from hq.domains.expenses.models import Expense
from hq.domains.receipts.models import Receipt
from hq.domains.receipts.validation import MAX_RECEIPT_BYTES, validate_receipt_file
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.record_errors import InputError
from hq.platform.application.tests.markup import elements, links, select, text


def _receipt(**fields):
    return Receipt.objects.create(file="receipts/example.pdf", content_type="application/pdf", **fields)


def _edit_post(**overrides):
    """A receipt edit the form accepts as it stands: every required field, and no new file."""

    return {"vendor": "Example Host", "amount": "12.00", "notes": "Paid by card", **overrides}


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

        found = links(response)
        # The row names the vendor, so the expense is named by its item.
        self.assertIn((self.expense.item, self.expense.get_absolute_url()), found)
        self.assertIn(("Example Switch", self.asset.get_absolute_url()), found)
        # A receipt with no vendor is named by its file.
        self.assertIn("scan.pdf", text(response))
        self.assertContains(response, "No expense or asset")
        self.assertNotContains(response, ">expense</a>")

    def test_a_match_s_row_links_it_to_this_receipt(self):
        receipt = _receipt(vendor="Example Host", amount=Decimal("12.00"))
        url = reverse("receipts:match", args=[receipt.pk])

        (button,) = select(self.client.get(url), "formaction", url)

        self.assertEqual((button.attrs["name"], button.attrs["value"]), ("expense_id", str(self.expense.pk)))
        self.assertTrue(any("row-actions" in ancestor.classes for ancestor in button.ancestors))

    def test_a_linked_receipt_says_what_it_is_linked_to(self):
        receipt = _receipt(vendor="Example Host", related_expense=self.expense)

        response = self.client.get(reverse("receipts:match", args=[receipt.pk]))

        self.assertContains(response, "Already linked to")
        # Said on its own, the expense names who was paid as well as for what.
        self.assertIn((f"{self.expense.vendor} {self.expense.item}", self.expense.get_absolute_url()), links(response))

    def test_with_no_expense_to_link_it_offers_to_add_one_from_the_receipt(self):
        receipt = _receipt(vendor="Example Registrar", amount=Decimal("8.00"))

        response = self.client.get(reverse("receipts:match", args=[receipt.pk]))

        (line,) = select(response, "data-empty-state")
        (add,) = [link for link in elements(response, "a") if line in link.ancestors]
        self.assertEqual(add.attrs["href"], response.context["new_expense"])

    def test_uploading_from_an_expense_starts_on_that_expense(self):
        response = self.client.get(reverse("receipts:create"), {"related_expense": self.expense.pk})

        self.assertEqual(response.context["form"].initial["related_expense"], self.expense.pk)

    def test_a_new_expense_from_a_receipt_starts_with_what_the_receipt_says(self):
        response = self.client.get(reverse("expenses:create"), {"vendor": "Example Host", "cost": "12.00"})

        self.assertEqual(response.context["form"].initial["vendor"], "Example Host")
        self.assertEqual(response.context["form"].initial["total_cost"], "12.00")

    def test_the_edit_form_links_the_current_file_through_the_protected_view(self):
        receipt = _receipt(original_filename="scan.pdf")

        response = self.client.get(reverse("receipts:edit", args=[receipt.pk]))

        self.assertIn(("scan.pdf", reverse("receipts:file", args=[receipt.pk])), links(response))
        self.assertNotContains(response, "/_internal-media/")


class ReceiptFileRuleTests(SimpleTestCase):
    def test_a_file_over_the_limit_is_refused_in_megabytes(self):
        upload = SimpleUploadedFile("big.pdf", b"", content_type="application/pdf")
        upload.size = MAX_RECEIPT_BYTES + 1024 * 1024

        with self.assertRaises(InputError) as refused:
            validate_receipt_file(upload)

        self.assertEqual(refused.exception.reason, "This file is 16 MB. The limit is 15 MB.")


class ReceiptEditTests(TestCase):
    """An edit holds a file to the receipt file rules only when it brings a new one."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("receipt-editor"))
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        storage = Receipt._meta.get_field("file").storage
        patcher = mock.patch.object(storage, "location", media.name)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.receipt = Receipt.objects.create(vendor="Example Host", content_type="application/pdf")
        self.receipt.file.save("scan.pdf", ContentFile(b"%PDF-1.4"))

    def _edit(self, **fields):
        return self.client.post(reverse("receipts:edit", args=[self.receipt.pk]), form_keyed(_edit_post(**fields)))

    def test_an_edit_without_a_new_file_saves(self):
        stored = self.receipt.file.name

        response = self._edit()

        self.assertRedirects(response, self.receipt.get_absolute_url(), fetch_redirect_response=False)
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.notes, "Paid by card")
        self.assertEqual(self.receipt.file.name, stored)

    def test_an_edit_with_a_new_file_of_a_refused_type_is_refused(self):
        stored = self.receipt.file.name

        response = self._edit(file=SimpleUploadedFile("note.html", b"markup", content_type="text/html"))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].has_error("file"))
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.file.name, stored)
        self.assertEqual(self.receipt.notes, "")

    def test_an_edit_with_a_new_valid_file_saves_it(self):
        stored = self.receipt.file.name

        response = self._edit(file=SimpleUploadedFile("photo.png", b"\x89PNG", content_type="image/png"))

        self.assertRedirects(response, self.receipt.get_absolute_url(), fetch_redirect_response=False)
        self.receipt.refresh_from_db()
        self.assertNotEqual(self.receipt.file.name, stored)
        self.assertEqual(self.receipt.original_filename, "photo.png")
        self.assertEqual(self.receipt.content_type, "image/png")


class ReceiptPageFileTests(TestCase):
    """A receipt's page shows its file beside its facts: a picture of an
    image, a link to anything else, and what is wrong when there is none."""

    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("receipt-viewer"))
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        storage = Receipt._meta.get_field("file").storage
        patcher = mock.patch.object(storage, "location", media.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _stored(self, name: str, content_type: str, body: bytes = b"bytes") -> Receipt:
        receipt = Receipt.objects.create(
            vendor="Example Host", original_filename=name, content_type=content_type, size_bytes=len(body)
        )
        receipt.file.save(name, ContentFile(body))
        return receipt

    def _file_part(self, response) -> str:
        (part,) = select(response, "data-receipt-file")
        return part.attrs["data-receipt-file"]

    def test_an_image_is_drawn_from_the_picture_view(self):
        receipt = self._stored("photo.png", "image/png")

        response = self.client.get(receipt.get_absolute_url())

        self.assertEqual(self._file_part(response), "picture")
        (picture,) = elements(response, "img")
        self.assertEqual(picture.attrs["src"], reverse("receipts:picture", args=[receipt.pk]))
        self.assertTrue(picture.attrs["alt"])

    def test_the_picture_is_served_inline_as_its_stored_type_and_not_audited(self):
        from hq.platform.core.models import AuditLog

        receipt = self._stored("photo.png", "image/png", b"\x89PNG")
        before = AuditLog.objects.count()

        response = self.client.get(reverse("receipts:picture", args=[receipt.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/png")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response["Content-Disposition"].startswith("inline"))
        self.assertEqual(b"".join(response.streaming_content), b"\x89PNG")
        # The page view is the read; only taking the file away is recorded.
        self.assertEqual(AuditLog.objects.count(), before)
        # Read to the end, as a server would, which closes the file.
        b"".join(self.client.get(reverse("receipts:file", args=[receipt.pk])).streaming_content)
        self.assertEqual(AuditLog.objects.count(), before + 1)

    def test_a_speculative_request_for_the_picture_is_answered(self):
        receipt = self._stored("photo.png", "image/png")

        response = self.client.get(reverse("receipts:picture", args=[receipt.pk]), headers={"Sec-Purpose": "prefetch"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"bytes")

    def test_the_picture_needs_a_session(self):
        receipt = self._stored("photo.png", "image/png")
        self.client.logout()

        response = self.client.get(reverse("receipts:picture", args=[receipt.pk]))

        self.assertNotEqual(response.status_code, 200)

    def test_a_pdf_is_a_link_to_the_download_never_framed(self):
        receipt = self._stored("scan.pdf", "application/pdf", b"%PDF-1.4")

        response = self.client.get(receipt.get_absolute_url())

        self.assertEqual(self._file_part(response), "link")
        self.assertIn(("scan.pdf", reverse("receipts:file", args=[receipt.pk])), links(response))
        self.assertFalse(elements(response, "img") + elements(response, "iframe") + elements(response, "object"))
        self.assertIn("PDF", text(response))
        self.assertNotContains(response, "application/pdf")
        self.assertEqual(self.client.get(reverse("receipts:picture", args=[receipt.pk])).status_code, 404)

    def test_an_image_no_browser_draws_is_a_link(self):
        receipt = self._stored("photo.heic", "image/heic")

        response = self.client.get(receipt.get_absolute_url())

        self.assertEqual(self._file_part(response), "link")
        self.assertFalse(elements(response, "img"))
        self.assertEqual(self.client.get(reverse("receipts:picture", args=[receipt.pk])).status_code, 404)

    def test_an_image_stored_under_an_unaccepted_type_is_never_a_picture(self):
        receipt = self._stored("page.svg", "image/svg+xml", b"<svg/>")

        response = self.client.get(receipt.get_absolute_url())

        self.assertFalse(elements(response, "img"))
        self.assertEqual(self.client.get(reverse("receipts:picture", args=[receipt.pk])).status_code, 404)

    def test_a_receipt_with_no_file_says_so_and_offers_one(self):
        receipt = Receipt.objects.create(vendor="Example Host")

        response = self.client.get(receipt.get_absolute_url())

        self.assertEqual(self._file_part(response), "none")
        self.assertIn(("Add a file", reverse("receipts:edit", args=[receipt.pk])), links(response))
        self.assertNotIn(reverse("receipts:file", args=[receipt.pk]), [href for _, href in links(response)])

    def test_a_missing_file_says_it_is_gone_and_offers_no_download(self):
        receipt = _receipt(vendor="Example Host", original_filename="lost.pdf")

        response = self.client.get(receipt.get_absolute_url())

        self.assertEqual(self._file_part(response), "missing")
        self.assertIn("lost.pdf", text(response))
        self.assertIn(("Replace the file", reverse("receipts:edit", args=[receipt.pk])), links(response))
        self.assertNotIn(reverse("receipts:file", args=[receipt.pk]), [href for _, href in links(response)])
        self.assertFalse(elements(response, "img"))
