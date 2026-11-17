from pathlib import Path
from typing import override

from django.conf import settings

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq_sdk import template_contract

HEAD = """# Shared UI

What an extension's templates may use of the host's: the frames and their
blocks, the public partials and what each reads, the host tags and filters,
and the classes and `data-` hooks. Written by `manage.py shared_ui_reference`
from the host's templates (`hq_sdk.template_contract`); a test fails when this
file falls behind. The rules around it are in
[Plugins](Plugins.md#shared-ui-contract).

"""


def shared_ui_doc() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "Shared-UI.md"


class Command(DerivedFileCommand):
    help = "Write docs/Shared-UI.md from the host's templates, or --check it for drift."
    source = "the host's templates"

    @override
    def target(self) -> Path:
        return shared_ui_doc()

    @override
    def derive(self) -> str:
        return HEAD + template_contract.markdown(template_contract.describe())
