from pathlib import Path
from typing import override

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq.platform.api.openapi import DOCUMENT_PATH, document, render


class Command(DerivedFileCommand):
    help = "Write hq-api.openapi.json from the API's routes and registries, or --check it for drift."
    source = "the API's routes and registries"

    @override
    def target(self) -> Path:
        return DOCUMENT_PATH

    @override
    def derive(self) -> str:
        return render(document())
