"""Load every stored value a declared shape reads, and name each one refused.

The release rehearsal runs it on its copy of production's database, so a
release whose code refuses a value production holds is refused itself. Exit
status 1 when any value is refused.
"""

from typing import Any, override

from django.core.management.base import BaseCommand, CommandError

from hq.platform.application.ui import counted
from hq.platform.core.stored_json import refusals


class Command(BaseCommand):
    help = "Load every stored value through its column's declared shape, naming each one refused."

    @override
    def handle(self, *args: Any, **options: Any) -> None:
        refused = 0
        for refusal in refusals():
            self.stdout.write(str(refusal))
            refused += 1
        if refused:
            raise CommandError(
                f"{counted(refused, 'stored value is', 'stored values are')} refused by the shape their column declares."
            )
