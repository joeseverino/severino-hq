"""Receipts."""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.urls import reverse

from core.models import TimestampedModel

from .storage import PrivateReceiptStorage, receipt_upload_path


class Receipt(TimestampedModel):
    """A receipt file linked to an expense and/or an asset."""

    file = models.FileField(
        upload_to=receipt_upload_path,
        storage=PrivateReceiptStorage(),
        help_text="PDF / image / screenshot. Stored privately; not exposed publicly.",
    )
    original_filename = models.CharField(max_length=255, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    size_bytes = models.PositiveIntegerField(default=0)

    vendor = models.CharField(max_length=160, blank=True)
    date = models.DateField(null=True, blank=True)
    amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    notes = models.TextField(blank=True)

    related_expense = models.ForeignKey(
        "expenses.Expense",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
    )
    related_asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="receipts",
    )

    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-uploaded_at",)
        indexes = [
            models.Index(fields=("-uploaded_at",)),
            models.Index(fields=("-date",)),
            models.Index(fields=("vendor",)),
        ]

    def __str__(self) -> str:
        label = self.original_filename or (self.file.name if self.file else "receipt")
        return f"{self.vendor or 'Receipt'} — {label}"

    def get_absolute_url(self) -> str:
        return reverse("receipts:detail", args=[self.pk])
