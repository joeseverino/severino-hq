import sqlite3
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.platform.application.search import apply_search, global_search, search_ids, search_records
from hq.platform.application.security import AuthorizationError, cli_principal, mcp_principal
from hq.domains.control_plane.models import ManagedResource
from hq.platform.core.models import AuditLog
from hq.domains.projects.models import Project

from .backends import _fts_query, search_backend, snippet_parts
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

    def test_a_save_that_leaves_the_body_alone_writes_nothing(self):
        from hq.platform.core import revisions

        project = Project.objects.create(name="Quiet save")
        document = SearchDocument.objects.get(scope="projects", object_id=project.slug)
        table = SearchDocument._meta.db_table
        before = revisions.read().counts[table]

        with CaptureQueriesContext(connection) as queries:
            project.save(update_fields=["updated_at"])

        self.assertEqual(revisions.read().counts[table], before)
        self.assertEqual(
            [query["sql"] for query in queries if table in query["sql"]][1:], []
        )
        self.assertEqual(
            SearchDocument.objects.get(pk=document.pk).updated_at, document.updated_at
        )

    def test_the_full_text_entry_is_rewritten_only_when_the_body_changes(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = 'search_document_au'"
            )
            (sql,) = cursor.fetchone()
        self.assertIn("AFTER UPDATE OF body", sql)
        self.assertIn("WHEN old.body IS NOT new.body", sql)

        project = Project.objects.create(name="Lighthouse")
        document = SearchDocument.objects.filter(scope="projects", object_id=project.slug)
        # An update that restates the body leaves the entry as it stands.
        document.update(body=document.get().body)
        self.assertEqual(search_ids("projects", "lighthouse", principal=OPERATOR), [project.slug])
        document.update(body="harbour")
        self.assertEqual(search_ids("projects", "lighthouse", principal=OPERATOR), [])
        self.assertEqual(search_ids("projects", "harbour", principal=OPERATOR), [project.slug])
        with connection.cursor() as cursor:
            cursor.execute("INSERT INTO search_index_fts(search_index_fts) VALUES ('integrity-check')")

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

    def test_query_plan_uses_the_fts_virtual_table(self):
        with connection.cursor() as cursor:
            cursor.execute(
                "EXPLAIN QUERY PLAN SELECT rowid FROM search_index_fts "
                "WHERE search_index_fts MATCH %s",
                ['"oper"*'],
            )
            plan = " ".join(str(column) for row in cursor.fetchall() for column in row)

        self.assertIn("VIRTUAL TABLE INDEX", plan)


# The ranked read in its per-scope form: one statement for one scope. It is the
# reference the single statement over every scope reproduces hit for hit.
_PER_SCOPE_REFERENCE_SQL = """
    SELECT document.object_id,
           snippet(search_index_fts, 0, char(2), char(3), ' … ', 16)
    FROM search_index_fts AS search
    JOIN search_index_searchdocument AS document
      ON document.id = search.rowid
    WHERE search_index_fts MATCH %s AND document.scope = %s
    ORDER BY search.rank, document.object_id
    LIMIT %s
"""


def _reference_hits(scope: str, query: str, limit: int) -> list:
    with connection.cursor() as cursor:
        cursor.execute(_PER_SCOPE_REFERENCE_SQL, [_fts_query(query), scope, limit])
        return [(row[0], snippet_parts(row[1])) for row in cursor.fetchall()]


def _ranks(scope: str, query: str) -> dict[str, float]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT document.object_id, search.rank FROM search_index_fts AS search "
            "JOIN search_index_searchdocument AS document ON document.id = search.rowid "
            "WHERE search_index_fts MATCH %s AND document.scope = %s",
            [_fts_query(query), scope],
        )
        return dict(cursor.fetchall())


def _tied_projects() -> list[Project]:
    """Projects whose bodies differ only in one token, created out of id order."""
    return [Project.objects.create(name=f"Tied ledger {letter}") for letter in "cadb"]


class RankedOrderTests(TestCase):
    def test_hits_of_equal_rank_are_ordered_by_object_id(self):
        projects = _tied_projects()
        slugs = sorted(project.slug for project in projects)

        self.assertEqual(len(set(_ranks("projects", "ledger").values())), 1)
        self.assertNotEqual([project.slug for project in projects], slugs)
        self.assertEqual(search_ids("projects", "ledger", principal=OPERATOR), slugs)
        group = next(
            group
            for group in global_search("ledger", principal=OPERATOR)["groups"]
            if group["scope"] == "projects"
        )
        self.assertEqual([item["id"] for item in group["items"]], slugs)
        records = search_records("projects", "ledger", principal=OPERATOR)
        self.assertEqual([item["id"] for item in records["items"]], slugs)
        listed = apply_search(
            Project.objects.all(), scope="projects", query="ledger", principal=OPERATOR
        ).order_by("_search_rank", "pk")
        self.assertEqual([project.slug for project in listed], slugs)

    def test_a_better_rank_comes_before_a_lower_object_id(self):
        Project.objects.create(name="Aardvark", notes="ledger " + "filler " * 40)
        best = Project.objects.create(name="Zebra ledger ledger ledger")

        ranks = _ranks("projects", "ledger")

        self.assertLess(ranks[best.slug], ranks["aardvark"])
        self.assertEqual(
            search_ids("projects", "ledger", principal=OPERATOR),
            [best.slug, "aardvark"],
        )

    def test_a_tie_in_a_numeric_scope_orders_its_ids_as_text(self):
        """Object ids are text in the index, so 10 sorts before 9: the rule is
        one comparison for every scope, not one per identifier type."""
        logs = [
            AuditLog.objects.create(action="login", object_type="user", message="tied entry")
            for _ in range(11)
        ]
        expected = sorted(str(log.pk) for log in logs)

        self.assertEqual(search_ids("audit", "tied", principal=OPERATOR), expected)
        listed = apply_search(
            AuditLog.objects.all(), scope="audit", query="tied", principal=OPERATOR
        ).order_by("_search_rank", "pk")
        self.assertEqual([str(log.pk) for log in listed], expected)


class SingleStatementSearchTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _tied_projects()
        for index in range(12):
            Project.objects.create(
                name=f"Ledger project {index}",
                description="ledger " * (index % 4) + "archive notes",
                technologies_used="example sqlite" if index % 2 else "example",
            )
            ManagedResource.objects.create(
                key=f"example-ledger-{index}",
                kind="example.device",
                spec={"name": f"Ledger node {index}", "note": "archive " * (index % 3)},
            )
            AuditLog.objects.create(
                action="login", object_type="user", message=f"ledger archive {index % 3}"
            )

    def test_one_statement_returns_what_the_per_scope_statements_return(self):
        scopes = list(SearchDocument.objects.values_list("scope", flat=True).distinct())
        self.assertGreaterEqual(len(scopes), 3)
        for query in ("ledger", "archive", "example", "ledger archive", "led"):
            for limit in (1, 3, 8, 100):
                with self.subTest(query=query, limit=limit):
                    expected = {
                        scope: hits
                        for scope in scopes
                        if (hits := _reference_hits(scope, query, limit))
                    }
                    self.assertTrue(expected)
                    self.assertEqual(
                        search_backend.search_scopes(
                            scopes=scopes, query=query, limit=limit
                        ),
                        expected,
                    )

    def test_one_scope_is_the_same_statement_as_many(self):
        many = search_backend.search_scopes(
            scopes=["projects", "audit"], query="ledger", limit=5
        )
        one = search_backend.search_scopes(scopes=["projects"], query="ledger", limit=5)

        self.assertEqual(one, {"projects": many["projects"]})
        self.assertEqual(
            search_backend.search(scope="projects", query="ledger", limit=5),
            [object_id for object_id, _ in many["projects"]],
        )

    def test_global_search_reads_the_index_once_and_fetches_only_hit_scopes(self):
        search_ids("projects", "tied", principal=OPERATOR)

        # One read of the index, one record fetch for each scope with a hit.
        with self.assertNumQueries(3) as queries:
            outcome = global_search("tied", principal=OPERATOR)

        self.assertEqual(
            sorted(group["scope"] for group in outcome["groups"] if group["count"]),
            ["audit", "projects"],
        )
        statements = [query["sql"] for query in queries]
        self.assertEqual(
            len([sql for sql in statements if "search_index_fts" in sql]), 1
        )
        self.assertEqual(len([sql for sql in statements if "projects_project" in sql]), 1)
        self.assertEqual(len([sql for sql in statements if "core_auditlog" in sql]), 1)

    def test_a_scope_without_a_hit_is_not_fetched(self):
        with CaptureQueriesContext(connection) as queries:
            outcome = global_search("ledger", principal=OPERATOR)

        empty = {group["scope"] for group in outcome["groups"] if not group["count"]}
        self.assertLessEqual({"expenses", "receipts", "assets"}, empty)
        statements = [query["sql"] for query in queries]
        self.assertEqual(
            len([sql for sql in statements if "search_index_fts" in sql]), 1
        )
        for table in ("expenses_expense", "receipts_receipt", "assets_asset"):
            self.assertFalse([sql for sql in statements if table in sql])

    def test_a_search_with_no_hit_costs_one_statement(self):
        search_ids("projects", "tied", principal=OPERATOR)

        with self.assertNumQueries(1):
            outcome = global_search("zzzznomatchhere", principal=OPERATOR)

        self.assertEqual(outcome["total"], 0)

    def test_scopes_the_principal_lacks_are_not_read_from_the_index(self):
        with mock.patch.object(
            search_backend, "search_scopes", wraps=search_backend.search_scopes
        ) as read:
            outcome = global_search("ledger", principal=mcp_principal())

        scopes = read.call_args.kwargs["scopes"]
        self.assertNotIn("audit", scopes)
        self.assertIn("projects", scopes)
        self.assertNotIn("audit", {group["scope"] for group in outcome["groups"]})
        with CaptureQueriesContext(connection) as queries:
            global_search("ledger", principal=mcp_principal())
        self.assertFalse(
            [query["sql"] for query in queries if "audit" in query["sql"]]
        )

    def test_fallback_orders_and_omits_like_the_index_path(self):
        with mock.patch(
            "hq.platform.application.search._fts5_available", return_value=False
        ):
            with CaptureQueriesContext(connection) as queries:
                outcome = global_search("ledger", principal=mcp_principal())
            listed = apply_search(
                Project.objects.all(),
                scope="projects",
                query="ledger",
                principal=OPERATOR,
            ).order_by("_search_rank", "pk")
            nothing = apply_search(
                Project.objects.all(),
                scope="projects",
                query="zzzznomatchhere",
                principal=OPERATOR,
            ).order_by("_search_rank", "pk")

            self.assertEqual(
                [project.pk for project in listed],
                sorted(
                    Project.objects.filter(name__icontains="ledger").values_list(
                        "pk", flat=True
                    )
                ),
            )
            self.assertEqual(list(nothing), [])
        scopes = {group["scope"] for group in outcome["groups"]}
        self.assertIn("projects", scopes)
        self.assertNotIn("audit", scopes)
        self.assertFalse(
            [query["sql"] for query in queries if "search_index_fts" in query["sql"]]
        )
        self.assertFalse(
            [query["sql"] for query in queries if "core_auditlog" in query["sql"]]
        )


class SearchedListTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        _tied_projects()
        for index in range(30):
            Project.objects.create(
                name=f"Ledger project {index}",
                description="ledger " * (index % 5),
            )

    def _listed(self, query: str = "ledger"):
        return apply_search(
            Project.objects.all(), scope="projects", query=query, principal=OPERATOR
        ).order_by("_search_rank", "pk")

    def test_every_match_is_ranked_in_the_index_order(self):
        ranked = search_ids("projects", "ledger", principal=OPERATOR)

        listed = list(self._listed())

        self.assertEqual(len(ranked), 34)
        self.assertEqual([project.slug for project in listed], ranked)
        self.assertEqual(
            [project._search_rank for project in listed], list(range(1, 35))
        )

    def test_a_page_is_one_statement_whose_size_does_not_follow_the_matches(self):
        # The first search in a process also asks whether the index exists.
        tied, ledger = self._listed("tied"), self._listed("ledger")
        with CaptureQueriesContext(connection) as few:
            list(tied[:25])
        with CaptureQueriesContext(connection) as many:
            list(ledger[:25])

        self.assertEqual(len(few), 1)
        self.assertEqual(len(many), 1)
        self.assertNotIn("CASE", many[0]["sql"])
        self.assertEqual(
            len(many[0]["sql"]) - len(few[0]["sql"]), 2 * (len("ledger") - len("tied"))
        )

    def test_count_and_page_agree(self):
        with self.assertNumQueries(1):
            self.assertEqual(self._listed().count(), 34)

    def test_the_result_ceiling_keeps_the_best_hits(self):
        ranked = search_ids("projects", "ledger", principal=OPERATOR)

        with mock.patch("hq.platform.application.search.MAX_SEARCH_RESULTS", 5):
            listed = list(self._listed())

        self.assertEqual([project.slug for project in listed], ranked[:5])

    def test_the_search_composes_with_other_filters_and_orderings(self):
        matching = self._listed().filter(name__startswith="Tied")

        self.assertEqual(matching.count(), 4)
        self.assertEqual(
            [project.name for project in matching.order_by("-name")],
            ["Tied ledger d", "Tied ledger c", "Tied ledger b", "Tied ledger a"],
        )
        self.assertEqual(
            Project.objects.filter(pk__in=self._listed().values("pk")).count(), 34
        )

    def test_an_empty_query_is_an_empty_orderable_result(self):
        with self.assertNumQueries(0):
            self.assertEqual(list(self._listed("  ")), [])

    def test_a_list_cannot_search_a_scope_its_principal_lacks(self):
        with self.assertRaises(AuthorizationError):
            apply_search(
                AuditLog.objects.all(),
                scope="audit",
                query="login",
                principal=mcp_principal(),
            )


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

    def test_infrastructure_kind_finds_real_local_records_without_provider_io(self):
        resource = ManagedResource.objects.create(
            key="example-tailnet-node",
            kind="tailscale.device",
            spec={"name": "Example node"},
        )

        outcome = global_search("tailscale", principal=OPERATOR)

        group = next(
            item
            for item in outcome["groups"]
            if item["scope"] == "infrastructure.resources"
        )
        (item,) = group["items"]
        self.assertEqual(item["title"], resource.key)
        self.assertEqual(item["badge"], resource.kind_label)
        self.assertNotEqual(item["badge"], "tailscale.device")
        self.assertEqual(item["url"], resource.get_absolute_url())

    def test_a_structured_field_is_indexed_as_words_not_as_python(self):
        """The snippet under a result is cut out of the body. ``str()`` on a
        JSONField would put ``{'key_expiry_disabled': False}`` there, and the
        punctuation carrying the meaning is what the tokenizer discards, so
        the key and its value would be indexed as unrelated words."""
        ManagedResource.objects.create(
            key="example-node",
            kind="example.device",
            spec={"hostname": "alpha", "key_expiry_disabled": False, "tags": []},
            conditions=[{"type": "Ready", "status": "True"}],
        )

        body = SearchDocument.objects.get(
            scope="infrastructure.resources", object_id="example-node"
        ).body

        self.assertIn("hostname: alpha", body)
        self.assertIn("key_expiry_disabled: no", body)
        self.assertIn("type: Ready", body)
        for python in ("{", "}", "[", "]", "'", "False"):
            self.assertNotIn(python, body)

    def test_a_multi_line_value_is_indexed_on_one_line(self):
        """A snippet window is a fixed number of characters around the match.
        Spent on escaped newlines, it shows whitespace instead of context."""
        ManagedResource.objects.create(
            key="example-policy",
            kind="example.policy",
            spec={"document": '{\r\n  "grants": [\r\n    {"dst": ["*"]}\r\n  ]\r\n}'},
        )

        body = SearchDocument.objects.get(
            scope="infrastructure.resources", object_id="example-policy"
        ).body

        (document,) = [
            line for line in body.splitlines() if line.startswith("document:")
        ]
        self.assertNotIn("\r", body)
        self.assertIn('"grants"', document)
        self.assertNotIn("  ", document)

    def test_docs_use_title_not_str_and_carry_doc_id_badge(self):
        from hq.domains.docs_index.models import DocumentationRecord

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

        with mock.patch("hq.platform.application.search._fts5_available", return_value=False):
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
