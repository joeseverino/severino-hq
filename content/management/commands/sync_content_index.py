"""Pull the jseverino.com content index into ContentItems.

Run on a daily systemd timer (deploy/systemd/severino-hq-content-sync.*) and
on demand from the project "Refresh" button. The DB is the cache between runs.

    python manage.py sync_content_index
    python manage.py sync_content_index --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from content.content_sync import ContentSyncError, sync_content_index


class Command(BaseCommand):
    help = "Pull the jseverino.com content index into ContentItems."

    def add_arguments(self, parser):
        parser.add_argument(
            "--json",
            action="store_true",
            help="Print stats as JSON for wrapper CLIs.",
        )

    def handle(self, *args, **options):
        try:
            stats = sync_content_index()
        except ContentSyncError as exc:
            raise CommandError(str(exc)) from exc

        if options["json"]:
            self.stdout.write(json.dumps(stats))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"Content index synced: {stats['total']} item(s) "
                f"({stats['created']} new, {stats['updated']} updated) "
                f"→ project {stats['project'] or '(none found)'}"
            )
        )
