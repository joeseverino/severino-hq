from pathlib import Path
from typing import override

from hq.platform.core.management.derived import DerivedFileCommand
lazy from hq.platform.application import procedure_schema


class Command(DerivedFileCommand):
    help = (
        "Write hq-procedure.schema.json from the model a procedure's sidecar is validated by, or --check it for drift."
    )
    source = "ProcedureDeclaration"

    @override
    def emitted(self) -> dict[Path, str]:
        return {procedure_schema.SCHEMA_PATH: procedure_schema.render()}
