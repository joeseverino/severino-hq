"""A record of the import document is its command's fields, each optional but the slug."""

from dataclasses import fields

from django.test import SimpleTestCase

from ..assets import AssetCommand
from ..projects import ProjectCommand
from ..registry_import import ImportedAsset, ImportedProject


class ImportedRecordTests(SimpleTestCase):
    def test_each_record_takes_its_commands_fields_and_requires_only_the_slug(self):
        for record, command in ((ImportedProject, ProjectCommand), (ImportedAsset, AssetCommand)):
            with self.subTest(record.__name__):
                self.assertEqual(
                    record.__required_keys__ | record.__optional_keys__, {item.name for item in fields(command)}
                )
                self.assertEqual(record.__required_keys__, {"slug"})
