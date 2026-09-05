from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Write hq_sdk/contract.json from the SDK's exports, or --check it for drift."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit 1 when the committed contract differs from the exports.",
        )

    def handle(self, *args, **options):
        from hq_sdk.contract import (
            CONTRACT_PATH,
            describe,
            drift,
            load_committed,
            render,
        )

        current = describe()
        if options["check"]:
            differences = drift(load_committed(), current)
            if differences:
                for line in differences:
                    self.stderr.write(line)
                raise CommandError(
                    f"{CONTRACT_PATH.name} is behind hq_sdk; "
                    "run `manage.py sdk_contract`, review the diff, and decide "
                    "whether PLUGIN_API_VERSION must move."
                )
            self.stdout.write("hq_sdk contract is current.")
            return
        CONTRACT_PATH.write_text(render(current), encoding="utf-8")
        self.stdout.write(f"Wrote {CONTRACT_PATH}.")
