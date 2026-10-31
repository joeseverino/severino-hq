"""Receipt writes, each through its capability: an upload, a replaced file, a
changed detail and a link to an expense."""

import base64
import hashlib
import json
import tempfile
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from hq.domains.expenses.models import Expense
from hq.domains.receipts import validation
from hq.domains.receipts.models import Receipt
from hq.domains.receipts.validation import MAX_RECEIPT_BASE64_CHARS
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.idempotency import IdempotencyRecord
from hq.platform.application.receipts import REPLACE, UPDATE, UPLOAD
from hq.platform.application.security import OPERATOR_CAPABILITIES, Capability, Principal, cli_principal
from hq.platform.application.tests.markup import text
from hq.platform.core.models import AuditLog

GRANTED = frozenset(Capability) | OPERATOR_CAPABILITIES
# Tokens granted everything, which a file still never arrives with.
CREDENTIALS = (
    Principal("example-agent", "mcp", GRANTED, granted=GRANTED),
    Principal("example-client", "api", GRANTED, granted=GRANTED),
)
# Tokens that may read receipts and not change them.
READERS = (
    Principal("example-agent", "mcp", frozenset({Capability.READ})),
    Principal("example-client", "api", frozenset({Capability.READ})),
)
# Bytes no row but the receipt's file may hold, and their base64.
CONTENT = b"%PDF-1.4 example-receipt-bytes"
ENCODED = base64.b64encode(CONTENT).decode("ascii")


def denials(capability: str) -> int:
    return AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=capability).count()


def as_principal(principal: Principal):
    """The receipt views' caller, as ``principal``."""

    return mock.patch("hq.domains.receipts.views.web_principal", return_value=principal)


def operator_without(capability: str) -> Principal:
    return Principal("op", "web", OPERATOR_CAPABILITIES - {capability})


def a_file(content_type: str = "application/pdf", content: str = ENCODED) -> dict[str, str]:
    return {"name": "scan.pdf", "content_type": content_type, "content": content}


class _PrivateMedia(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("op"))
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        patcher = mock.patch.object(Receipt._meta.get_field("file").storage, "location", media.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def stored(self) -> Receipt:
        receipt = Receipt.objects.get()
        receipt.file.open("rb")
        self.addCleanup(receipt.file.close)
        return receipt


class UploadCapabilityTests(_PrivateMedia):
    def upload(self, file, principal=None, **payload):
        return execute_capability(
            UPLOAD, {"vendor": "Example Host", "file": file, **payload}, principal=principal or cli_principal()
        )

    def test_neither_the_audit_trail_nor_the_retry_record_holds_the_file(self):
        result = self.upload(a_file(), idempotency_key="example-upload-1")

        self.assertTrue(result["ok"], result)
        self.assertEqual(self.stored().file.read(), CONTENT)
        digest = hashlib.sha256(CONTENT).hexdigest()
        told = {"name": "scan.pdf", "size_bytes": len(CONTENT), "sha256": digest}
        self.assertEqual(result["file"], told)
        uploaded = AuditLog.objects.get(action=AuditLog.Action.UPLOADED)
        self.assertEqual({name: uploaded.metadata[name] for name in told}, told)
        kept = [json.dumps([row.message, row.metadata]) for row in AuditLog.objects.all()] + [
            json.dumps([row.request_sha256, row.response]) for row in IdempotencyRecord.objects.all()
        ]
        self.assertTrue(IdempotencyRecord.objects.exists())
        for row in kept:
            self.assertNotIn(ENCODED, row)
            self.assertNotIn("example-receipt-bytes", row)

    def test_base64_longer_than_the_limit_encodes_to_is_refused_before_it_is_decoded(self):
        with mock.patch.object(base64, "b64decode", wraps=base64.b64decode) as decode:
            result = self.upload(a_file(content="A" * (MAX_RECEIPT_BASE64_CHARS + 4)))

        self.assertEqual(result["error"]["code"], "invalid_input")
        decode.assert_not_called()
        self.assertFalse(Receipt.objects.exists())

    def test_the_limit_holds_the_decoded_size(self):
        content = b"%PDF-1.4 x"
        with mock.patch.object(validation, "MAX_RECEIPT_BYTES", len(content)):
            within = self.upload(a_file(content=base64.b64encode(content).decode()))
            over = self.upload(a_file(content=base64.b64encode(content + b"x").decode()))

        # The encoded text of each is longer than the limit; only the bytes count.
        self.assertTrue(within["ok"], within)
        self.assertEqual(over["error"], {"code": "invalid_input", "message": "This file is 11 B. The limit is 10 B."})
        self.assertEqual(Receipt.objects.count(), 1)

    def test_a_file_a_receipt_cannot_be_is_refused_in_the_hosts_words(self):
        for file, said in (
            (a_file("text/html"), "A receipt cannot be this type of file. Upload a PDF or an image."),
            (a_file(content="not base64!"), "The file's content is not base64."),
        ):
            with self.subTest(said=said):
                self.assertEqual(self.upload(file)["error"], {"code": "invalid_input", "message": said})
        self.assertFalse(Receipt.objects.exists())

    def test_a_token_is_refused_whatever_it_was_granted(self):
        for principal in CREDENTIALS:
            with self.subTest(interface=principal.interface):
                result = self.upload(a_file(), principal)

                self.assertEqual(result["error"]["code"], "forbidden")
                self.assertNotIn(ENCODED, str(result))
        self.assertFalse(Receipt.objects.exists())
        self.assertEqual(denials(UPLOAD), 2)


class CreateViewTests(_PrivateMedia):
    def post(self, content_type="application/pdf", content=CONTENT):
        return self.client.post(
            reverse("receipts:create"),
            {
                "vendor": "Example Host",
                "amount": "12.00",
                "notes": "",
                "file": SimpleUploadedFile("scan.pdf", content, content_type=content_type),
            },
        )

    def test_an_upload_is_kept_and_recorded_under_the_operator(self):
        response = self.post()

        receipt = self.stored()
        self.assertRedirects(response, receipt.get_absolute_url(), fetch_redirect_response=False)
        self.assertEqual(receipt.file.read(), CONTENT)
        uploaded = AuditLog.objects.get(action=AuditLog.Action.UPLOADED)
        self.assertEqual((uploaded.metadata["actor"], uploaded.metadata["operation"]), ("op", UPLOAD))
        self.assertEqual(uploaded.metadata["sha256"], hashlib.sha256(CONTENT).hexdigest())

    def test_a_token_and_an_operator_without_the_grant_are_refused_on_the_form(self):
        for principal in (*CREDENTIALS, operator_without(Capability.UPLOAD_RECEIPT)):
            with self.subTest(interface=principal.interface), as_principal(principal):
                response = self.post()

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].non_field_errors())
        self.assertFalse(Receipt.objects.exists())
        self.assertEqual(denials(UPLOAD), 3)

    def test_a_file_a_receipt_cannot_be_is_refused_on_the_form(self):
        for content_type, content, said in (
            ("text/html", b"markup", "A receipt cannot be this type of file."),
            ("application/pdf", b"x" * (validation.MAX_RECEIPT_BYTES + 1), "The limit is 15 MB."),
        ):
            with self.subTest(content_type=content_type):
                response = self.post(content_type, content)

                self.assertEqual(response.status_code, 200)
                self.assertIn(said, text(response))
        self.assertFalse(Receipt.objects.exists())

    def test_the_capabilitys_refusal_is_said_on_the_form(self):
        refusal = {"ok": False, "error": {"code": "not_found", "message": "A related expense or asset was not found."}}
        with mock.patch("hq.domains.receipts.views.execute_capability", return_value=refusal):
            response = self.post()

        self.assertIn("A related expense or asset was not found.", text(response))


class EditViewTests(_PrivateMedia):
    def setUp(self):
        super().setUp()
        self.receipt = Receipt.objects.create(vendor="Example Host", content_type="application/pdf")
        self.receipt.file.save("old.pdf", ContentFile(b"%PDF-1.4 old"))
        self.url = reverse("receipts:edit", args=[self.receipt.pk])
        self.old = self.receipt.file.name

    def post(self, **fields):
        return self.client.post(self.url, {"vendor": "Example Host", "amount": "12.00", "notes": "Paid", **fields})

    def a_new_file(self, content_type="image/png", content=b"\x89PNG new"):
        return SimpleUploadedFile("photo.png", content, content_type=content_type)

    def test_a_new_file_replaces_the_old_and_is_recorded(self):
        response = self.post(file=self.a_new_file())

        self.assertRedirects(response, self.receipt.get_absolute_url(), fetch_redirect_response=False)
        receipt = self.stored()
        self.assertEqual((receipt.original_filename, receipt.notes), ("photo.png", "Paid"))
        self.assertEqual(receipt.file.read(), b"\x89PNG new")
        uploaded = AuditLog.objects.get(action=AuditLog.Action.UPLOADED)
        self.assertEqual(uploaded.metadata["operation"], REPLACE)
        self.assertNotIn(base64.b64encode(b"\x89PNG new").decode(), json.dumps(uploaded.metadata))

    def test_details_alone_are_the_records_update(self):
        response = self.post()

        self.assertRedirects(response, self.receipt.get_absolute_url(), fetch_redirect_response=False)
        updated = AuditLog.objects.filter(action=AuditLog.Action.UPDATED, object_type="Receipt").latest("pk")
        self.assertEqual((updated.metadata["actor"], updated.metadata["operation"]), ("op", UPDATE))
        self.assertEqual(updated.metadata["changes"]["notes"], ["", "Paid"])
        self.assertFalse(AuditLog.objects.filter(action=AuditLog.Action.UPLOADED).exists())

    def test_a_token_and_an_operator_without_the_grant_are_refused_on_the_form(self):
        for capability, callers, fields in (
            (REPLACE, (*CREDENTIALS, operator_without(Capability.UPLOAD_RECEIPT)), lambda: {"file": self.a_new_file()}),
            (UPDATE, (*READERS, operator_without("write_receipts")), dict),
        ):
            for principal in callers:
                with self.subTest(capability=capability, interface=principal.interface), as_principal(principal):
                    response = self.post(**fields())

                    self.assertEqual(response.status_code, 200)
                    self.assertTrue(response.context["form"].non_field_errors())
            self.assertEqual(denials(capability), 3)
        self.receipt.refresh_from_db()
        self.assertEqual((self.receipt.notes, self.receipt.file.name), ("", self.old))

    def test_a_new_file_a_receipt_cannot_be_is_refused_on_the_form(self):
        for file in (
            self.a_new_file("text/html", b"markup"),
            self.a_new_file(content=b"x" * (validation.MAX_RECEIPT_BYTES + 1)),
        ):
            with self.subTest(content_type=file.content_type):
                response = self.post(file=file)

                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["form"].has_error("file"))
        self.receipt.refresh_from_db()
        self.assertEqual(self.receipt.notes, "")

    def test_the_capabilitys_refusal_is_said_on_the_form(self):
        refusal = {"ok": False, "error": {"code": "conflict", "message": "That receipt changed after it was read."}}
        with mock.patch("hq.domains.receipts.views.execute_capability", return_value=refusal):
            response = self.post(file=self.a_new_file())

        self.assertIn("That receipt changed after it was read.", text(response))


class MatchViewTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("op"))
        self.receipt = Receipt.objects.create(
            file="receipts/example.pdf", content_type="application/pdf", vendor="Example Host", notes="Kept"
        )
        self.expense = Expense.objects.create(
            date="2030-01-02", vendor="Example Host", item="Hosting", total_cost=Decimal("12.00")
        )
        self.url = reverse("receipts:match", args=[self.receipt.pk])

    def link(self):
        return self.client.post(self.url, {"expense_id": self.expense.pk}, follow=True)

    def test_a_link_keeps_the_details_and_is_recorded_under_the_operator(self):
        response = self.link()

        self.assertIn(f"Receipt linked to {self.expense.label}", text(response))
        self.receipt.refresh_from_db()
        self.assertEqual((self.receipt.related_expense, self.receipt.notes), (self.expense, "Kept"))
        updated = AuditLog.objects.get(action=AuditLog.Action.UPDATED, object_type="Receipt")
        self.assertEqual((updated.metadata["actor"], updated.metadata["operation"]), ("op", UPDATE))

    def test_a_token_and_an_operator_without_the_grant_are_refused_with_the_reason(self):
        for principal in (*READERS, operator_without("write_receipts")):
            with self.subTest(interface=principal.interface), as_principal(principal):
                response = self.link()

                self.assertNotIn("Receipt linked", text(response))
        self.receipt.refresh_from_db()
        self.assertIsNone(self.receipt.related_expense)
        self.assertEqual(denials(UPDATE), 3)

    def test_a_refused_link_says_why(self):
        refusal = {"ok": False, "error": {"code": "conflict", "message": "That receipt changed after it was read."}}
        with mock.patch("hq.domains.receipts.views.execute_capability", return_value=refusal):
            response = self.link()

        self.assertIn("That receipt changed after it was read.", text(response))
