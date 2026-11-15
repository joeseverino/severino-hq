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
    def target(self) -> Path:
        return procedure_schema.SCHEMA_PATH

    @override
    def derive(self) -> str:
        return procedure_schema.render()
