"""Create or update an Asset record from the CLI.

Idempotent — re-running with the same slug updates the existing record.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from assets.models import (
    ASSET_CATEGORY_CHOICES,
    PAYMENT_METHOD_CHOICES,
    Asset,
)


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CommandError(f"Invalid date {value!r}; use YYYY-MM-DD.") from exc


def _parse_money(value: str) -> Decimal:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise CommandError(f"Invalid amount {value!r}.") from exc


class Command(BaseCommand):
    help = "Create or update an Asset record by slug."

    def add_arguments(self, parser):
        parser.add_argument("slug", help="URL slug (e.g. jseverino-com).")
        parser.add_argument(
            "--name",
            required=True,
            dest="item_name",
            help="Display name (free text).",
        )
        parser.add_argument(
            "--category",
            choices=[c[0] for c in ASSET_CATEGORY_CHOICES],
            default="other",
        )
        parser.add_argument(
            "--status",
            choices=[c.value for c in Asset.Status],
            default=Asset.Status.ACTIVE,
        )
        parser.add_argument("--vendor", default="")
        parser.add_argument(
            "--purchase-date",
            dest="purchase_date",
            default=None,
            help="ISO date YYYY-MM-DD.",
        )
        parser.add_argument(
            "--cost",
            dest="total_cost",
            default=None,
            help="Total cost as a decimal (e.g. 199.00).",
        )
        parser.add_argument(
            "--business-use",
            dest="business_use_percentage",
            type=int,
            default=100,
            help="0-100. Business-use percentage.",
        )
        parser.add_argument(
            "--payment",
            dest="payment_method",
            choices=[c[0] for c in PAYMENT_METHOD_CHOICES],
            default="",
        )
        parser.add_argument("--serial", dest="serial_number", default="")
        parser.add_argument(
            "--warranty-date",
            dest="warranty_date",
            default=None,
            help="ISO date YYYY-MM-DD.",
        )
        parser.add_argument("--notes", default="")

    def handle(self, *args, **opts):
        slug = opts["slug"]
        defaults = {
            "item_name": opts["item_name"],
            "vendor": opts["vendor"],
            "category": opts["category"],
            "status": opts["status"],
            "business_use_percentage": opts["business_use_percentage"],
            "payment_method": opts["payment_method"],
            "serial_number": opts["serial_number"],
            "notes": opts["notes"],
        }
        if opts["purchase_date"]:
            defaults["purchase_date"] = _parse_date(opts["purchase_date"])
        if opts["warranty_date"]:
            defaults["warranty_date"] = _parse_date(opts["warranty_date"])
        if opts["total_cost"] is not None:
            defaults["total_cost"] = _parse_money(opts["total_cost"])

        obj, created = Asset.objects.update_or_create(
            slug=slug, defaults=defaults
        )
        verb = "created" if created else "updated"
        self.stdout.write(f"Asset {obj.slug}: {verb}")
