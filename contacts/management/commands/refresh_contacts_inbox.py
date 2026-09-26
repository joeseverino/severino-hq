"""Read the contact inbox state from D1 and store it."""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from contacts import inbox


class Command(BaseCommand):
    help = "Read the unread count and newest submissions from D1 and store them."

    def handle(self, *args, **options):
        inbox.refresh(force=True)
        count, status = inbox.unread()
        self.stdout.write(json.dumps({"unread": count, "status": status}, sort_keys=True))
