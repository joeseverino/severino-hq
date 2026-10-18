"""The search index data migrations, run over the rows each one meets."""

from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from hq.domains.projects.models import Project
from hq.platform.core.tests.migration_steps import MigrationStepTestCase
from hq.platform.search_index.registry import BY_MODEL


def leaf(app_label):
    (found,) = MigrationExecutor(connection).loader.graph.leaf_nodes(app_label)
    return found


def body_trigger() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'search_document_au'")
        (sql,) = cursor.fetchone()
    return sql


class SearchIndexMigrationTests(MigrationStepTestCase):
    def test_resources_are_indexed_and_old_bodies_become_readable(self):
        apps = self.migrate(("search_index", "0001_initial"), leaf("control_plane"), leaf("projects"))
        apps.get_model("control_plane", "ManagedResource").objects.create(
            key="example-box", kind="machine", spec={"name": "example-box"}
        )
        Projects = apps.get_model("projects", "Project")
        indexed = Projects.objects.create(name="Example Project", slug="example-project", description="Readable.")
        Projects.objects.create(name="Unindexed", slug="unindexed")

        apps = self.migrate(("search_index", "0002_managed_resources"), leaf("control_plane"), leaf("projects"))
        Documents = apps.get_model("search_index", "SearchDocument")
        resource = Documents.objects.get(scope="infrastructure.resources")
        self.assertEqual(resource.object_id, "example-box")
        self.assertIn("machine", resource.body)

        scope = BY_MODEL[Project].scope
        Documents.objects.create(scope=scope, object_id=indexed.slug, body=str({"name": "Example Project"}))

        apps = self.migrate(("search_index", "0003_readable_search_bodies"), leaf("projects"))
        Documents = apps.get_model("search_index", "SearchDocument")
        rebuilt = Documents.objects.get(scope=scope)
        self.assertIn("Example Project", rebuilt.body)
        self.assertNotIn("{", rebuilt.body)
        self.assertFalse(Documents.objects.filter(scope=scope, object_id="unindexed").exists())

    def test_only_a_changed_body_rewrites_the_full_text_entry(self):
        self.migrate(("search_index", "0003_readable_search_bodies"))
        self.assertNotIn("UPDATE OF body", body_trigger())

        apps = self.migrate(("search_index", "0004_fts_update_when_body_changes"))
        self.assertIn("WHEN old.body IS NOT new.body", body_trigger())

        Documents = apps.get_model("search_index", "SearchDocument")
        document = Documents.objects.create(scope="example", object_id="1", body="first")
        Documents.objects.filter(pk=document.pk).update(body="second")
        with connection.cursor() as cursor:
            cursor.execute("SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH 'second'")
            self.assertEqual(cursor.fetchall(), [(document.pk,)])
            cursor.execute("SELECT rowid FROM search_index_fts WHERE search_index_fts MATCH 'first'")
            self.assertEqual(cursor.fetchall(), [])
