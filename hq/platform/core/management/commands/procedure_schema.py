from typing import override

from django.core.management.base import BaseCommand, CommandError

lazy from hq.platform.application import procedure_schema


class Command(BaseCommand):
    help = (
        "Write hq-procedure.schema.json from the model a procedure's sidecar is validated by, or --check it for drift."
    )

    @override
    def add_arguments(self, parser):
        parser.add_argument(
            "--check", action="store_true", help="Exit 1 when the committed schema is behind the model."
        )

    @override
    def handle(self, *args, **options):
        current = procedure_schema.render()
        path = procedure_schema.SCHEMA_PATH
        if options["check"]:
            if not path.exists() or path.read_text(encoding="utf-8") != current:
                raise CommandError(
                    f"{path.name} is behind ProcedureDeclaration; run `mise run manage -- procedure_schema` "
                    "and commit the result."
                )
            self.stdout.write(f"{path.name} is current.")
            return
        path.write_text(current, encoding="utf-8")
        self.stdout.write(f"Wrote {path.name}.")
