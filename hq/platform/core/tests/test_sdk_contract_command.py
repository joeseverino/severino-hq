"""`manage.py sdk_contract --check` fails on each record it keeps when that record is stale, and writes nothing."""

import copy
import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from hq.platform.core.management.commands import sdk_contract as command
from hq_sdk import contract, template_contract, text_contract


def stale_contract(current: dict) -> str:
    """The contract with one export gone, as a record from before it was added reads."""

    behind = copy.deepcopy(current)
    exports = next(module for module in behind["modules"].values() if module)
    exports.pop(min(exports))
    return contract.render(behind)


def stale_examples(current: dict) -> str:
    """The text examples with one rendering reworded."""

    behind = copy.deepcopy(current)
    example = next(examples for examples in behind.values() if examples)[0]
    example["output"] = f"{example['output']} (reworded)"
    return text_contract.render(behind)


class CheckTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.contract = contract.describe()
        cls.examples = text_contract.describe()
        cls.section = template_contract.markdown(cls.contract["templates"])

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.paths = {
            "contract": Path(folder.name, "contract.json"),
            "examples": Path(folder.name, "text_examples.json"),
            "doc": Path(folder.name, "Plugins.md"),
        }
        self.current = {
            "contract": contract.render(self.contract),
            "examples": text_contract.render(self.examples),
            "doc": command.documented(f"# Plugins\n\n{command.BEGIN}\n{command.END}\n", self.section),
        }
        for patcher in (
            patch.object(contract, "CONTRACT_PATH", self.paths["contract"]),
            patch.object(text_contract, "EXAMPLES_PATH", self.paths["examples"]),
            patch.object(command, "plugins_doc", lambda: self.paths["doc"]),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def check(self, records: dict[str, str]) -> None:
        for name, text in records.items():
            self.paths[name].write_text(text, encoding="utf-8")
        call_command("sdk_contract", "--check", stdout=StringIO(), stderr=StringIO())

    def test_current_records_pass(self):
        self.check(self.current)

    def test_each_stale_record_fails_and_nothing_is_written(self):
        doc = self.current["doc"]
        stale = {
            "contract": stale_contract(json.loads(self.current["contract"])),
            "examples": stale_examples(self.examples),
            "doc": doc.replace(self.section, f"{self.section}\n| a row the templates lost |"),
        }
        for name, text in stale.items():
            records = {**self.current, name: text}
            with self.subTest(record=name), self.assertRaises(CommandError):
                self.check(records)
            for kept, written in records.items():
                self.assertEqual(self.paths[kept].read_text(encoding="utf-8"), written, kept)
