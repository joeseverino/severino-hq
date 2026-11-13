"""Automated reports keep the documentation boundary, including linked identifiers."""

import json

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.reports import exports
from hq.platform.application.security import AuthorizationError, Capability, Principal

from ..identity import set_principal
from ..services import export_year_summary

READER = Principal("example-export-reader", "mcp", frozenset({Capability.READ}))


class ExportSensitivityTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.docs = [
            DocumentationRecord.objects.create(
                doc_id=f"example-{level}",
                title=f"Example {level} document",
                sensitivity=level,
                obsidian_path=f"example/{level}.md",
            )
            for level in DocumentationRecord.Sensitivity.values
        ]
        post = ContentItem.objects.create(slug="example-post", title="Example post")
        post.related_documentation.add(*cls.docs)

    def exported(self, output_format):
        with set_principal(READER):
            return export_year_summary(2026, output_format)["content"]

    def test_json_contains_only_safe_documents_and_related_identifiers(self):
        data = json.loads(self.exported("json"))

        self.assertEqual({doc["doc_id"] for doc in data["documentation"]}, {"example-public", "example-internal"})
        self.assertEqual(set(data["content"][0]["related_documentation"]), {"example-public", "example-internal"})
        for marker in ("example-sensitive", "example-restricted", "example/sensitive.md", "example/restricted.md"):
            self.assertNotIn(marker, json.dumps(data))

    def test_markdown_withholds_unsafe_metadata_and_keeps_safe_titles(self):
        body = self.exported("md")

        for doc in self.docs:
            with self.subTest(sensitivity=doc.sensitivity):
                if doc.is_safe_for_ai_export:
                    self.assertIn(doc.title, body)
                else:
                    self.assertNotIn(doc.title, body)
                    self.assertNotIn(doc.doc_id, body)
                    self.assertNotIn(doc.obsidian_path, body)

    def test_an_empty_safe_scope_never_falls_back_to_all_documents(self):
        DocumentationRecord.objects.filter(sensitivity__in=("public", "internal")).delete()

        data = json.loads(self.exported("json"))
        self.assertEqual(data["documentation"], [])
        self.assertEqual(data["content"][0]["related_documentation"], [])

    def test_complete_browser_reports_keep_every_document(self):
        data = json.loads(exports.year_summary_json(2026))

        self.assertEqual({doc["doc_id"] for doc in data["documentation"]}, {doc.doc_id for doc in self.docs})
        for doc in self.docs:
            self.assertIn(doc.title, exports.year_summary_markdown(2026))

    def test_a_caller_without_read_is_refused(self):
        with set_principal(Principal("example-ungranted", "mcp", frozenset())), self.assertRaises(AuthorizationError):
            export_year_summary(2026, "json")

    def test_more_content_with_document_links_adds_no_queries(self):
        with CaptureQueriesContext(connection) as initial:
            self.exported("json")
        for index in range(8):
            post = ContentItem.objects.create(slug=f"example-extra-{index}", title=f"Example extra {index}")
            post.related_documentation.add(*self.docs)

        with CaptureQueriesContext(connection) as larger:
            data = json.loads(self.exported("json"))

        self.assertEqual(len(data["content"]), 9)
        self.assertEqual(len(larger), len(initial))
