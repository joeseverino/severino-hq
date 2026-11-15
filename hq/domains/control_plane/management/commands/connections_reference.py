from pathlib import Path
from typing import override

from django.conf import settings

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq.platform.application.connection_setup import reference_markdown


def reference_path() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "Connections.md"


class Command(DerivedFileCommand):
    help = "Write docs/Connections.md from the providers' declarations, or --check it for drift."
    source = "the providers"

    @override
    def target(self) -> Path:
        return reference_path()

    @override
    def derive(self) -> str:
        return reference_markdown()
