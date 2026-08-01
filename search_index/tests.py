import sqlite3
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase

from application.search import apply_search, global_search, search_ids, search_records
from application.security import AuthorizationError, cli_principal, mcp_principal
from core.models import AuditLog
from projects.models import Project

from .backends import snippet_parts
from .models import SearchDocument
from .services import rebuild_search_index

OPERATOR = cli_principal()


class IndexedSearchTests(TestCase):
    def test_prefix_search_tracks_create_update_and_delete(self):
        project = Project.objects.create(
            name="Operations console",
            technologies_used="Django SQLite",
        )

        self.assertEqual(
            search_ids("projects", "oper djan", principal=OPERATOR), [project.slug]
        )

        project.name = "Command center"
        project.technologies_used = "Python"
        project.save()
        self.assertEqual(search_ids("projects", "djan", principal=OPERATOR), [])
        self.assertEqual(
            search_ids("projects", "comm pyth", principal=OPERATOR), [project.slug]
        )

        project.delete()
        self.assertEqual(search_ids("projects", "comm", principal=OPERATOR), [])

    def test_rebuild_recovers_a_missing_projection(self):
        project = Project.objects.create(name="Recovery target")
        SearchDocument.objects.filter(scope="projects").delete()
        self.assertEqual(search_ids("projects", "recovery", principal=OPERATOR), [])

        counts = rebuild_search_index()

        self.assertEqual(counts["projects"], 1)
        self.assertEqual(
            search_ids("projects", "recovery", principal=OPERATOR), [project.slug]
        )

    def test_projection_and_fts_roll_back_with_domain_write(self):
        def create_then_fail():
            with transaction.atomic():
                Project.objects.create(name="Rolled back project")
                raise RuntimeError("force rollback")

        with self.assertRaises(RuntimeError):
            create_then_fail()

        self.assertEqual(search_ids("projects", "rolled", principal=OPERATOR), [])
        self.assertFalse(
            SearchDocument.objects.filter(scope="projects", body__icontains="rolled").exists()
        )

    def test_adapter_neutral_result_and_cli_are_json(self):
        project = Project.objects.create(name="Searchable HQ")

        result = search_records("projects", "search", principal=OPERATOR, limit=5)
        stdout = StringIO()
        call_command("search_hq", "projects", "search", stdout=stdout)

        (item,) = result["items"]
        self.assertEqual(item["id"], project.slug)
        self.assertEqual(item["label"], str(project))
        self.assertIn("Searchable", item["snippet"])
        self.assertIn('"scope": "projects"', stdout.getvalue())

    def test_relevance_window_keeps_tail_ordering_deterministic(self):
        projects = [
            Project.objects.create(name=f"Shared term {index}") for index in range(3)
        ]

        with mock.patch("application.search.RELEVANCE_WINDOW", 1):
            queryset = apply_search(
                Project.objects.all(),
                scope="projects",
                query="shared",
                principal=OPERATOR,
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


class GlobalSearchTests(TestCase):
    def test_snippet_parts_split_markers(self):
        self.assertEqual(
            snippet_parts("plain \x02hit\x03 tail"),
            [("plain ", False), ("hit", True), (" tail", False)],
        )

    def test_groups_rank_snippet_and_metadata(self):
        project = Project.objects.create(
            name="Vault engine",
            description="Frontmatter indexing for the operational vault",
        )

        outcome = global_search("vault", principal=OPERATOR)

        # The create is itself audited, so the query hits two scopes: the
        # project and its audit event. Cross-scope coverage is the point.
        scopes_with_hits = {g["scope"] for g in outcome["groups"] if g["count"]}
        self.assertEqual(scopes_with_hits, {"projects", "audit"})
        group = next(g for g in outcome["groups"] if g["scope"] == "projects")
        (item,) = group["items"]
        self.assertEqual(item["title"], "Vault engine")
        self.assertEqual(item["url"], project.get_absolute_url())
        self.assertIsNotNone(item["timestamp"])
        # Matched terms are flagged so the template can highlight safely.
        self.assertIn(True, [hit for _, hit in item["snippet"]])
        matched = "".join(t for t, hit in item["snippet"] if hit).lower()
        self.assertIn("vault", matched)

    def test_docs_use_title_not_str_and_carry_doc_id_badge(self):
        from docs_index.models import DocumentationRecord

        record = DocumentationRecord.objects.create(
            doc_id="rb-generate-homelab-cert",
            title="Generate a homelab certificate",
            doc_type="runbook",
        )

        outcome = global_search("homelab", principal=OPERATOR)

        group = next(g for g in outcome["groups"] if g["scope"] == "documentation")
        (item,) = group["items"]
        self.assertEqual(item["title"], record.title)
        self.assertEqual(item["badge"], record.doc_id)
        self.assertNotIn("—", item["title"])

    def test_scopes_the_principal_lacks_are_omitted_not_empty(self):
        AuditLog.objects.create(action="login", object_type="user", message="hq login")

        operator_scopes = {g["scope"] for g in global_search("hq", principal=OPERATOR)["groups"]}
        limited_scopes = {g["scope"] for g in global_search("hq", principal=mcp_principal())["groups"]}

        self.assertIn("audit", operator_scopes)
        self.assertNotIn("audit", limited_scopes)

    def test_fallback_backend_still_produces_marked_snippets(self):
        Project.objects.create(
            name="Fallback target",
            description="Portable snippet extraction without FTS",
        )

        with mock.patch("application.search._fts5_available", return_value=False):
            outcome = global_search("portable", principal=OPERATOR)

        group = next(g for g in outcome["groups"] if g["scope"] == "projects")
        (item,) = group["items"]
        marked = [t for t, hit in item["snippet"] if hit]
        self.assertEqual([m.lower() for m in marked], ["portable"])


class SearchAuthorizationTests(TestCase):
    def test_baseline_read_principal_cannot_search_the_audit_trail(self):
        AuditLog.objects.create(action="login", object_type="user", message="operator login")
        limited = mcp_principal()

        with self.assertRaises(AuthorizationError):
            search_ids("audit", "login", principal=limited)
        with self.assertRaises(AuthorizationError):
            search_records("audit", "login", principal=limited)

        # Baseline READ still covers ordinary record scopes.
        Project.objects.create(name="Reachable by MCP")
        self.assertEqual(
            len(search_ids("projects", "reachable", principal=limited)), 1
        )

    def test_operator_principal_searches_the_audit_trail(self):
        AuditLog.objects.create(action="login", object_type="user", message="operator login")

        self.assertEqual(len(search_ids("audit", "operator", principal=OPERATOR)), 1)


class SecureDeleteTests(TestCase):
    def test_fts_secure_delete_is_configured_when_supported(self):
        if sqlite3.sqlite_version_info < (3, 42, 0):
            self.skipTest("SQLite runtime predates FTS5 secure-delete")
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT v FROM search_index_fts_config WHERE k = 'secure-delete'"
            )
            row = cursor.fetchone()
        self.assertEqual(row, (1,))
