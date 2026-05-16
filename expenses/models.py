"""Expenses."""

from __future__ import annotations

from decimal import Decimal

from django.db import models

from assets.models import PAYMENT_METHOD_CHOICES, quantize_money
from core.models import TimestampedModel


EXPENSE_CATEGORY_CHOICES = [
    ("hosting", "Hosting"),
    ("domains", "Domains"),
    ("software", "Software"),
    ("hardware", "Hardware"),
    ("networking", "Networking gear"),
    ("office", "Office equipment"),
    ("education", "Education / training"),
    ("subscriptions", "Subscriptions"),
    ("content_production", "Content production"),
    ("consulting_tools", "Consulting tools"),
    ("professional_services", "Professional services"),
    ("miscellaneous", "Miscellaneous"),
]


class Expense(TimestampedModel):
    date = models.DateField()
    vendor = models.CharField(max_length=160)
    item = models.CharField(max_length=200)
    category = models.CharField(
        max_length=32, choices=EXPENSE_CATEGORY_CHOICES, default="miscellaneous"
    )
    total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    business_use_percentage = models.PositiveSmallIntegerField(
        default=100,
        help_text="0-100. Used to estimate the deductible amount.",
    )
    estimated_deductible_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        editable=False,
        help_text="Auto-calculated. Estimate only — not tax advice.",
    )
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True
    )
    business_purpose = models.CharField(
        max_length=300,
        blank=True,
        help_text="Short justification of the business reason for this expense.",
    )
    notes = models.TextField(blank=True)

    related_project = models.ForeignKey(
        "projects.Project",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
    )
    related_asset = models.ForeignKey(
        "assets.Asset",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
    )
    related_content = models.ForeignKey(
        "content.ContentItem",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
    )
    related_documentation = models.ForeignKey(
        "docs_index.DocumentationRecord",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="expenses",
    )

    class Meta:
        ordering = ("-date", "-id")
        indexes = [
            models.Index(fields=("-date",)),
            models.Index(fields=("category",)),
            models.Index(fields=("vendor",)),
        ]

    def __str__(self) -> str:
        return f"{self.date} {self.vendor} — {self.item}"

    def save(self, *args, **kwargs):
        pct = max(0, min(int(self.business_use_percentage or 0), 100))
        self.business_use_percentage = pct
        cost = self.total_cost or Decimal("0.00")
        self.estimated_deductible_amount = quantize_money(
            cost * Decimal(pct) / Decimal(100)
        )
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        from django.urls import reverse

        return reverse("expenses:detail", args=[self.pk])
