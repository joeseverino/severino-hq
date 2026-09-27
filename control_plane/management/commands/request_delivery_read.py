"""Ask the controller to read delivery now: run by the entrypoint as HQ boots."""

from __future__ import annotations

from django.core.management.base import BaseCommand

from application.cadence import request_delivery_read


class Command(BaseCommand):
    help = "Ask the controller for a github.delivery read, where delivery is read at all."

    def handle(self, *args, **options):
        asked = request_delivery_read()
        self.stdout.write("delivery read requested" if asked else "delivery is not read here")
