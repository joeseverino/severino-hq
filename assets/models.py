"""Assets / equipment."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from django.db import models
from django.urls import reverse
from django.utils.text import slugify

from core.models import TimestampedModel


PAYMENT_METHOD_CHOICES = [
    ("cash", "Cash"),
    ("debit", "Debit card"),
    ("credit", "Credit card"),
    ("ach", "ACH / bank transfer"),
    ("paypal", "PayPal"),
    ("other", "Other"),
]

ASSET_CATEGORY_CHOICES = [
    ("networking", "Networking gear"),
    ("server_hardware", "Server hardware"),
    ("software", "Software"),
    ("subscription", "Subscription"),
    ("domain", "Domain"),
    ("hosting", "Hosting"),
    ("camera_audio", "Camera / audio"),
    ("office", "Office equipment"),
    ("tools", "Tools"),
    ("training", "Training"),
    ("other", "Other"),
]


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


class Asset(TimestampedModel):
    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"
        SOLD = "sold", "Sold"
        RETURNED = "returned", "Returned"
        REPLACED = "replaced", "Replaced"

    item_name = models.CharField(max_length=160)
    slug = models.SlugField(max_length=180, unique=True, blank=True)
    vendor = models.CharField(max_length=120, blank=True)
    category = models.CharField(
        max_length=30, choices=ASSET_CATEGORY_CHOICES, default="other"
    )
    purchase_date = models.DateField(null=True, blank=True)
    total_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    business_use_percentage = models.PositiveSmallIntegerField(
        default=100,
        help_text="0-100. Multiplied into total_cost to estimate deductible amount.",
    )
    estimated_deductible_amount = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        editable=False,
        help_text=(
            "Auto-calculated from total_cost × business_use_percentage / 100. "
            "Estimate only — not tax advice."
        ),
    )
    payment_method = models.CharField(
        max_length=20, choices=PAYMENT_METHOD_CHOICES, blank=True
    )
    serial_number = models.CharField(max_length=120, blank=True)
    warranty_date = models.DateField(null=True, blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.ACTIVE
    )
    notes = models.TextField(blank=True)

    related_projects = models.ManyToManyField(
        "projects.Project", blank=True, related_name="assets"
    )

    class Meta:
        ordering = ("-purchase_date", "item_name")
        indexes = [
            models.Index(fields=("status",)),
            models.Index(fields=("category",)),
            models.Index(fields=("-purchase_date",)),
        ]

    def __str__(self) -> str:
        return self.item_name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.item_name) or "asset"
            slug = base
            n = 2
            while Asset.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug = f"{base}-{n}"
                n += 1
            self.slug = slug
        pct = max(0, min(int(self.business_use_percentage or 0), 100))
        self.business_use_percentage = pct
        cost = self.total_cost or Decimal("0.00")
        self.estimated_deductible_amount = quantize_money(
            cost * Decimal(pct) / Decimal(100)
        )
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("assets:detail", args=[self.slug])
