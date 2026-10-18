"""The docs index data migrations, run over the rows each one meets."""

from hq.platform.core.tests.migration_steps import MigrationStepTestCase

BEFORE = ("docs_index", "0002_initial")
AFTER = ("docs_index", "0003_rename_secret_adjacent_to_restricted")


class SensitivityRenameTests(MigrationStepTestCase):
    def sensitivities(self, apps):
        return dict(apps.get_model("docs_index", "DocumentationRecord").objects.values_list("doc_id", "sensitivity"))

    def test_secret_adjacent_becomes_restricted_and_comes_back_on_the_way_down(self):
        apps = self.migrate(BEFORE)
        Record = apps.get_model("docs_index", "DocumentationRecord")
        for doc_id, sensitivity in (("example-adjacent", "secret_adjacent"), ("example-internal", "internal")):
            Record.objects.create(doc_id=doc_id, title=doc_id, sensitivity=sensitivity)

        apps = self.migrate(AFTER)
        self.assertEqual(self.sensitivities(apps), {"example-adjacent": "restricted", "example-internal": "internal"})

        apps = self.migrate(BEFORE)
        self.assertEqual(
            self.sensitivities(apps), {"example-adjacent": "secret_adjacent", "example-internal": "internal"}
        )
