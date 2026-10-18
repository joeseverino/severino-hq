from pathlib import Path
from typing import override

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

BEGIN = "<!-- sdk_contract: shared UI contract -->"
END = "<!-- /sdk_contract -->"


def plugins_doc() -> Path:
    return Path(settings.BASE_DIR) / "docs" / "Plugins.md"


def documented(text: str, section: str) -> str:
    """docs/Plugins.md with its shared UI contract written from the record."""

    head, begin, rest = text.partition(BEGIN)
    _, end, tail = rest.partition(END)
    if not begin or not end:
        raise CommandError(f"Plugins.md has no {BEGIN} ... {END} section to write.")
    return f"{head}{BEGIN}\n\n{section}\n{END}{tail}"


class Command(BaseCommand):
    help = (
        "Write hq_sdk/contract.json, hq_sdk/text_examples.json and docs/Plugins.md's shared UI "
        "contract from the SDK and the host's templates, or --check them for drift."
    )

    @override
    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit 1 when a committed record differs from what the host offers.",
        )

    @override
    def handle(self, *args, **options):
        from hq_sdk import contract, template_contract, text_contract

        current = contract.describe()
        examples = text_contract.describe()
        doc = plugins_doc()
        text = doc.read_text(encoding="utf-8")
        written = documented(text, template_contract.markdown(current["templates"]))
        if options["check"]:
            differences = [
                *contract.drift(contract.load_committed(), current),
                *text_contract.drift(text_contract.load_committed(), examples),
                *(["~ docs/Plugins.md: shared UI contract"] if written != text else []),
            ]
            if differences:
                for line in differences:
                    self.stderr.write(line)
                raise CommandError(
                    "The SDK's committed record is behind the host; run `mise run regen`, "
                    "review the diff, and decide whether PLUGIN_API_VERSION must move."
                )
            self.stdout.write("hq_sdk contract is current.")
            return
        contract.CONTRACT_PATH.write_text(contract.render(current), encoding="utf-8")
        text_contract.EXAMPLES_PATH.write_text(text_contract.render(examples), encoding="utf-8")
        doc.write_text(written, encoding="utf-8")
        self.stdout.write(f"Wrote {contract.CONTRACT_PATH.name}, {text_contract.EXAMPLES_PATH.name} and {doc.name}.")
