"""Every named route and the path it answers, extensions' included.

    python manage.py routes [filter]

One line per route, ``name  /path/``, sorted by name. A filter keeps the lines
whose name or path contains it.
"""

from collections.abc import Iterator
from typing import override

from django.core.management.base import BaseCommand, CommandParser

from hq.platform.application.routes import routed


def named_routes() -> Iterator[tuple[str, str]]:
    """(name, path) for each named route, with namespaces and mounts joined."""

    return ((route.name, route.path) for route in routed() if route.name)


class Command(BaseCommand):
    help = "List every named route and its path; a filter keeps the matching ones."

    @override
    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("filter", nargs="?", default="")

    @override
    def handle(self, *args, **options):
        wanted = options["filter"]
        found = sorted(route for route in named_routes() if wanted in route[0] or wanted in route[1])
        width = max((len(name) for name, _path in found), default=0)
        for name, path in found:
            self.stdout.write(f"{name:<{width}}  {path}")
