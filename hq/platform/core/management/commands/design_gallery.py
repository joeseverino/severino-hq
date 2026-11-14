from pathlib import Path
from typing import override

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from hq.platform.core import design_gallery


class Command(BaseCommand):
    help = "Write the Design page's gallery of every shared part to SEVERINO_DESIGN_GALLERY, as the image build does."

    @override
    def add_arguments(self, parser):
        parser.add_argument("--output", type=Path, help="Where to write it instead of SEVERINO_DESIGN_GALLERY.")

    @override
    def handle(self, *args, **options):
        path = options["output"] or settings.SEVERINO_DESIGN_GALLERY
        if path is None:
            raise CommandError(
                "SEVERINO_DESIGN_GALLERY is unset, so this server draws the gallery live; name --output."
            )
        size = design_gallery.write(path)
        self.stdout.write(f"{path}: {size} bytes")
