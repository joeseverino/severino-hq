"""A managed record's search document follows its resource to the scope it is now named."""

from hq.platform.core.tests.migration_steps import MigrationStepTestCase


class RescopedRecordsTests(MigrationStepTestCase):
    def test_a_record_found_before_is_found_under_the_new_scope(self):
        apps = self.migrate(("search_index", "0005_drop_duplicate_index"))
        apps.get_model("search_index", "SearchDocument").objects.create(
            scope="infrastructure.resources", object_id="example-box", body="example-box"
        )
        apps = self.migrate(("search_index", "0006_records_scope_is_infrastructure"))
        documents = apps.get_model("search_index", "SearchDocument").objects
        self.assertEqual(list(documents.values_list("scope", "object_id")), [("infrastructure", "example-box")])
