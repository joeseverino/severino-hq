from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase

from application.search import apply_search, search_ids, search_records
from projects.models import Project

from .models import SearchDocument
from .services import rebuild_search_index


class IndexedSearchTests(TestCase):
    def test_prefix_search_tracks_create_update_and_delete(self):
        project = Project.objects.create(
            name="Operations console",
            technologies_used="Django SQLite",
        )

        self.assertEqual(search_ids("projects", "oper djan"), [project.slug])

        project.name = "Command center"
        project.technologies_used = "Python"
        project.save()
        self.assertEqual(search_ids("projects", "djan"), [])
        self.assertEqual(search_ids("projects", "comm pyth"), [project.slug])

        project.delete()
        self.assertEqual(search_ids("projects", "comm"), [])

    def test_rebuild_recovers_a_missing_projection(self):
        project = Project.objects.create(name="Recovery target")
        SearchDocument.objects.filter(scope="projects").delete()
        self.assertEqual(search_ids("projects", "recovery"), [])

        counts = rebuild_search_index()

        self.assertEqual(counts["projects"], 1)
        self.assertEqual(search_ids("projects", "recovery"), [project.slug])

    def test_projection_and_fts_roll_back_with_domain_write(self):
        with self.assertRaises(RuntimeError):
            with transaction.atomic():
                Project.objects.create(name="Rolled back project")
                raise RuntimeError("force rollback")

        self.assertEqual(search_ids("projects", "rolled"), [])
        self.assertFalse(
            SearchDocument.objects.filter(scope="projects", body__icontains="rolled").exists()
        )

    def test_adapter_neutral_result_and_cli_are_json(self):
        project = Project.objects.create(name="Searchable HQ")

        result = search_records("projects", "search", limit=5)
        stdout = StringIO()
        call_command("search_hq", "projects", "search", stdout=stdout)

        self.assertEqual(result["items"], [{"id": project.slug, "label": str(project)}])
        self.assertIn('"scope": "projects"', stdout.getvalue())

    def test_relevance_window_keeps_tail_ordering_deterministic(self):
        projects = [
            Project.objects.create(name=f"Shared term {index}") for index in range(3)
        ]

        with mock.patch("application.search.RELEVANCE_WINDOW", 1):
            queryset = apply_search(
                Project.objects.all(), scope="projects", query="shared"
            ).order_by("_search_rank", "pk")
            results = list(queryset)

        # Head of the window is ranked exactly; the tail shares one bucket
        # and must fall back to pk order, not drift between pages.
        self.assertEqual(len(results), 3)
        self.assertEqual(
            [project.pk for project in results[1:]],
            sorted(project.pk for project in projects if project != results[0]),
        )

    def test_query_plan_uses_the_fts_virtual_table(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "EXPLAIN QUERY PLAN SELECT rowid FROM search_index_fts "
                "WHERE search_index_fts MATCH %s",
                ['"oper"*'],
            )
            plan = " ".join(str(column) for row in cursor.fetchall() for column in row)

        self.assertIn("VIRTUAL TABLE INDEX", plan)
