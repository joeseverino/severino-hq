"""The one-time import: validated whole, applied whole, audited per record."""

from __future__ import annotations

import io
import json
from decimal import Decimal

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from assets.models import Asset
from core.models import AuditLog
from projects.models import Project

from .capabilities import capability_registry, execute_capability
from .registry_import import MAX_IMPORT_RECORDS, HQImportCommand, import_registry
from .security import Capability, Principal, cli_principal

DOCUMENT = {
    "projects": [
        {
            "slug": "example-site",
            "name": "Example site",
            "status": "active",
            "public_url": "https://www.example.com",
        },
        {"slug": "example-tool", "name": "Example tool"},
    ],
    "assets": [
        {
            "slug": "example-server",
            "item_name": "Example server",
            "total_cost": "420.00",
            "purchase_date": "2026-01-15",
            "related_projects": ["example-site"],
        }
    ],
}


def run(document=DOCUMENT, *, principal=None, check_only=False):
    return execute_capability(
        "hq.import",
        {**document, "check_only": check_only},
        principal=principal or cli_principal(),
    )


def imported_events():
    return AuditLog.objects.filter(action=AuditLog.Action.IMPORTED)


class ImportTests(TestCase):
    def test_a_document_imports_projects_and_assets(self):
        result = run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"]["created"], 3)
        asset = Asset.objects.get(slug="example-server")
        self.assertEqual(asset.total_cost, Decimal("420.00"))
        self.assertEqual(
            list(asset.related_projects.values_list("slug", flat=True)), ["example-site"]
        )

    def test_importing_twice_changes_nothing(self):
        run()
        before = {project.slug: project.updated_at for project in Project.objects.all()}

        result = run()

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["summary"], {"created": 0, "updated": 0, "unchanged": 3, "kept": 0})
        self.assertEqual(Project.objects.count(), 2)
        self.assertEqual(Asset.objects.count(), 1)
        after = {project.slug: project.updated_at for project in Project.objects.all()}
        self.assertEqual(before, after)

    def test_a_field_left_out_keeps_its_stored_value(self):
        run()

        run({"projects": [{"slug": "example-tool", "name": "Example tool", "notes": "kept"}]})
        run({"projects": [{"slug": "example-tool", "status": "paused"}]})

        project = Project.objects.get(slug="example-tool")
        self.assertEqual(project.notes, "kept")
        self.assertEqual(project.status, "paused")

    def test_one_audit_event_per_record_and_one_for_the_import(self):
        run()

        events = imported_events()
        self.assertEqual(events.count(), 4)
        self.assertEqual(events.filter(object_type="Project").count(), 2)
        self.assertEqual(events.filter(object_type="Asset").count(), 1)
        summary = events.get(object_type="Registry")
        self.assertEqual(summary.metadata["summary"]["created"], 3)
        self.assertEqual(summary.metadata["operation"], "hq.import")

    def test_a_repeat_import_is_still_audited(self):
        run()
        run()

        self.assertEqual(imported_events().count(), 8)


class ValidationTests(TestCase):
    def assert_refused(self, document, *fragments):
        result = run(document)
        self.assertFalse(result["ok"], result)
        text = json.dumps(result["problems"])
        for fragment in fragments:
            self.assertIn(fragment, text)
        self.assertFalse(Project.objects.exists())
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(imported_events().exists())
        return result

    def test_a_record_without_a_slug_is_refused(self):
        self.assert_refused({"projects": [{"name": "No slug"}]}, "slug is required")

    def test_a_slug_twice_is_refused(self):
        self.assert_refused(
            {"projects": [{"slug": "twice", "name": "A"}, {"slug": "twice", "name": "B"}]},
            "appears twice",
        )

    def test_an_unknown_field_is_refused(self):
        self.assert_refused(
            {"projects": [{"slug": "example", "name": "A", "hostname": "example.com"}]},
            "Unknown fields: hostname",
        )

    def test_a_bad_choice_is_refused_before_anything_is_written(self):
        self.assert_refused(
            {
                "projects": [
                    {"slug": "good", "name": "Good"},
                    {"slug": "bad", "name": "Bad", "status": "not-a-status"},
                ]
            },
            "not-a-status",
        )

    def test_an_asset_naming_an_unknown_project_is_refused(self):
        self.assert_refused(
            {"assets": [{"slug": "example", "item_name": "A", "related_projects": ["nowhere"]}]},
            "nowhere",
        )

    def test_every_problem_is_reported_at_once(self):
        result = self.assert_refused(
            {"projects": [{"name": "A"}, {"slug": "b", "name": "B", "category": "nope"}]}
        )
        self.assertEqual(len(result["problems"]), 2)

    def test_the_record_limit_is_enforced(self):
        document = {
            "projects": [
                {"slug": f"p-{index}", "name": "P"} for index in range(MAX_IMPORT_RECORDS + 1)
            ]
        }
        self.assert_refused(document, "the limit is")

    def test_a_record_refused_while_writing_rolls_back_every_record(self):
        """Model validation passes a record that the use case then refuses."""

        from dataclasses import replace
        from unittest import mock

        from . import registry_import

        def refuse(command, *, principal):
            raise registry_import.asset_use_cases.NotFoundError("refused while writing")

        refusing = replace(registry_import._ASSET, upsert=refuse)
        with mock.patch.object(registry_import, "_ASSET", refusing):
            result = run()

        self.assertFalse(result["ok"])
        self.assertIn("refused while writing", json.dumps(result["problems"]))
        self.assertFalse(Project.objects.exists())
        self.assertFalse(imported_events().exists())


class CheckOnlyTests(TestCase):
    def test_check_only_reports_and_writes_nothing(self):
        result = run(check_only=True)

        self.assertTrue(result["ok"], result)
        self.assertTrue(result["check_only"])
        self.assertEqual(result["summary"]["created"], 3)
        self.assertFalse(Project.objects.exists())
        self.assertFalse(AuditLog.objects.exists())


class DerivedFieldTests(TestCase):
    def test_a_stored_public_url_that_differs_is_kept_and_reported(self):
        Project.objects.create(
            slug="example-site", name="Example site", public_url="https://pages.example.com"
        )

        result = run()

        self.assertTrue(result["ok"], result)
        site = next(item for item in result["projects"] if item["slug"] == "example-site")
        self.assertEqual(
            site["kept"],
            [
                {
                    "field": "public_url",
                    "kept": "https://pages.example.com",
                    "offered": "https://www.example.com",
                }
            ],
        )
        self.assertEqual(
            Project.objects.get(slug="example-site").public_url, "https://pages.example.com"
        )
        self.assertEqual(result["summary"]["kept"], 1)

    def test_a_blank_public_url_is_filled(self):
        Project.objects.create(slug="example-site", name="Example site")

        run()

        self.assertEqual(
            Project.objects.get(slug="example-site").public_url, "https://www.example.com"
        )


class CapabilityGateTests(TestCase):
    def test_it_requires_what_both_upserts_require(self):
        spec = capability_registry()["hq.import"]
        upserts = set(capability_registry()["project.upsert"].required_capabilities) | set(
            capability_registry()["asset.upsert"].required_capabilities
        )

        self.assertEqual(set(spec.required_capabilities), upserts)

    def test_a_principal_missing_either_is_refused(self):
        for held in (Capability.WRITE_PROJECTS, Capability.WRITE_ASSETS):
            principal = Principal("example-agent", "mcp", frozenset({Capability.READ, held}))

            result = run(principal=principal)

            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "forbidden")
            self.assertFalse(Project.objects.exists())

    def test_the_use_case_refuses_without_the_capability_too(self):
        from .security import AuthorizationError

        principal = Principal("example-agent", "mcp", frozenset({Capability.WRITE_PROJECTS}))
        with self.assertRaises(AuthorizationError):
            import_registry(HQImportCommand(**DOCUMENT), principal=principal)


class ManagementCommandTests(TestCase):
    def write(self, document):
        import tempfile

        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(document, handle)
        handle.close()
        self.addCleanup(lambda: __import__("os").unlink(handle.name))
        return handle.name

    def test_the_command_imports_a_file(self):
        output = io.StringIO()

        call_command("import_registry", self.write(DOCUMENT), stdout=output)

        self.assertIn("Imported 2 projects and 1 asset", output.getvalue())
        self.assertEqual(Project.objects.count(), 2)

    def test_the_command_check_only_writes_nothing(self):
        output = io.StringIO()

        call_command("import_registry", self.write(DOCUMENT), "--check-only", stdout=output)

        self.assertIn("Would import", output.getvalue())
        self.assertFalse(Project.objects.exists())

    def test_the_command_fails_on_a_refused_document(self):
        with self.assertRaises(CommandError):
            call_command(
                "import_registry",
                self.write({"projects": [{"name": "No slug"}]}),
                stderr=io.StringIO(),
            )
