"""Create an expense through the canonical application service."""

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError

from application.expenses import ExpenseCommand, save_expense
from application.security import cli_principal


class Command(BaseCommand):
    help = "Create an Expense record."

    def add_arguments(self, parser):
        parser.add_argument("--date", required=True)
        parser.add_argument("--vendor", required=True)
        parser.add_argument("--item", required=True)
        parser.add_argument("--category", default="miscellaneous")
        parser.add_argument("--cost", required=True)
        parser.add_argument("--business-use", type=int, default=100)
        parser.add_argument("--payment", default="")
        parser.add_argument("--purpose", default="")
        parser.add_argument("--notes", default="")
        parser.add_argument("--project")
        parser.add_argument("--asset")
        parser.add_argument("--content")
        parser.add_argument("--doc")
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        try:
            expense_date = date.fromisoformat(options["date"])
            total_cost = Decimal(options["cost"])
        except (ValueError, InvalidOperation) as exc:
            raise CommandError("Date must be YYYY-MM-DD and cost must be decimal.") from exc
        result = save_expense(
            ExpenseCommand(
                date=expense_date,
                vendor=options["vendor"],
                item=options["item"],
                category=options["category"],
                total_cost=total_cost,
                business_use_percentage=options["business_use"],
                payment_method=options["payment"],
                business_purpose=options["purpose"],
                notes=options["notes"],
                related_project=options["project"],
                related_asset=options["asset"],
                related_content=options["content"],
                related_documentation=options["doc"],
            ),
            principal=cli_principal(),
        )
        if options["json"]:
            self.stdout.write(json.dumps(result))
        else:
            self.stdout.write(f"Expense {result['expense']['id']}: created")
