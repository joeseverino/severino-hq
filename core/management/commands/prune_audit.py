"""Delete routine machine audit events past their retention window.

Only the (action, object type) pairs in ``core.audit.ROUTINE_EVENTS`` are
eligible. Prints counts only.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from application.ui import counted
from core.audit import prune_routine, record_operation
from core.models import AuditLog


class Command(BaseCommand):
    help = "Delete routine audit events older than SEVERINO_AUDIT_ROUTINE_DAYS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Count what would be deleted and delete nothing.",
        )
        parser.add_argument("--batch", type=int, default=1000)

    def handle(self, *args, **options):
        days = int(settings.SEVERINO_AUDIT_ROUTINE_DAYS)
        if options["check_only"]:
            count = prune_routine(days=days, check_only=True)
            self.stdout.write(
                f"{counted(count, 'routine event', 'routine events')} older than "
                f"{counted(days, 'day')} would be deleted."
            )
            return
        deleted = prune_routine(days=days, batch=max(1, options["batch"]))
        if deleted:
            record_operation(
                "audit.prune",
                f"Deleted {counted(deleted, 'routine event', 'routine events')} older than "
                f"{counted(days, 'day')}.",
                action=AuditLog.Action.DELETED,
                metadata={"deleted": deleted, "days": days},
            )
        self.stdout.write(
            f"{counted(deleted, 'routine event', 'routine events')} older than {counted(days, 'day')} deleted."
        )
