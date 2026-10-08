"""Delete routine machine audit events past their retention window.

Only the (action, object type) pairs in ``core.audit.ROUTINE_EVENTS`` are
eligible. Prints counts only. The daily run is the ``audit.prune`` job
(``application.scheduled_work``); this is the same work by hand, and the way
to count what it would delete.
"""

from django.conf import settings
from django.core.management.base import BaseCommand

from hq.platform.application.scheduled_work import prune_audit
from hq.platform.application.ui import counted
from hq.platform.core.audit import prune_routine


class Command(BaseCommand):
    help = "Delete routine audit events older than SEVERINO_AUDIT_ROUTINE_DAYS."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check-only",
            action="store_true",
            help="Count what would be deleted and delete nothing.",
        )

    def handle(self, *args, **options):
        days = int(settings.SEVERINO_AUDIT_ROUTINE_DAYS)
        if options["check_only"]:
            count = prune_routine(days=days, check_only=True)
            self.stdout.write(
                f"{counted(count, 'routine event', 'routine events')} older than "
                f"{counted(days, 'day')} would be deleted."
            )
            return
        deleted = prune_audit()["deleted"]
        self.stdout.write(
            f"{counted(deleted, 'routine event', 'routine events')} older than {counted(days, 'day')} deleted."
        )
