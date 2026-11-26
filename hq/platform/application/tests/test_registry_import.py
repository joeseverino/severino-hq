"""The one-time import: validated whole, applied whole, audited per record."""

import json
from decimal import Decimal

from django.test import TestCase

from hq.domains.assets.models import Asset
from hq.domains.projects.models import Project
from hq.platform.core.models import AuditLog

from ..capabilities import command_spec, execute_capability
from ..registry_import import MAX_IMPORT_RECORDS
from ..security import cli_principal

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
        self.assertEqual(list(asset.related_projects.values_list("slug", flat=True)), ["example-site"])

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
        text = json.dumps(result["error"]["details"])
        for fragment in fragments:
            self.assertIn(fragment, text)
        self.assertFalse(Project.objects.exists())
        self.assertFalse(Asset.objects.exists())
        self.assertFalse(imported_events().exists())
        return result

    def test_a_slug_twice_is_refused(self):
        self.assert_refused(
            {"projects": [{"slug": "twice", "name": "A"}, {"slug": "twice", "name": "B"}]},
            "appears twice",
        )

    def test_an_asset_naming_an_unknown_project_is_refused(self):
        self.assert_refused(
            {"assets": [{"slug": "example", "item_name": "A", "related_projects": ["nowhere"]}]},
            "nowhere",
        )

    def test_every_problem_is_reported_at_once(self):
        shape = self.assert_refused({"projects": [{"name": "A"}, {"slug": "b", "name": "B", "hostname": "nope"}]})
        self.assertEqual(len(shape["error"]["details"]), 2)
        content = self.assert_refused(
            {"projects": [{"slug": "a", "name": "A", "status": "nope"}, {"slug": "b", "name": "B", "category": "nope"}]}
        )
        self.assertEqual(len(content["error"]["details"]), 2)

    def test_a_refusal_answers_the_code_of_what_it_depends_on(self):
        cases = (
            # A record no document may hold, whatever HQ stores.
            ({"projects": [{"slug": "twice", "name": "A"}, {"slug": "twice", "name": "B"}]}, "invalid_input"),
            # A record naming one HQ does not have.
            ({"assets": [{"slug": "example", "item_name": "A", "related_projects": ["nowhere"]}]}, "not_found"),
            # A new record lacking what creating one needs.
            ({"assets": [{"slug": "example"}]}, "precondition"),
            # The first cause any record holds, in that order.
            (
                {
                    "projects": [{"slug": "twice", "name": "A"}, {"slug": "twice", "name": "B"}],
                    "assets": [{"slug": "example", "item_name": "A", "related_projects": ["nowhere"]}],
                },
                "invalid_input",
            ),
            (
                {"assets": [{"slug": "example", "item_name": "A", "related_projects": ["nowhere"]}, {"slug": "new"}]},
                "not_found",
            ),
        )
        for document, code in cases:
            with self.subTest(code=code, document=document):
                self.assertEqual(self.assert_refused(document)["error"]["code"], code)

    def test_the_record_limit_is_enforced(self):
        document = {"projects": [{"slug": f"p-{index}", "name": "P"} for index in range(MAX_IMPORT_RECORDS + 1)]}
        self.assert_refused(document, "the limit is")

    def test_a_record_refused_while_writing_rolls_back_every_record(self):
        """Model validation passes a record that the use case then refuses."""

        from unittest import mock

        from .. import assets
        from ..record_errors import RecordNotFound

        with mock.patch.object(assets, "save_asset", side_effect=RecordNotFound("refused while writing")):
            result = run()

        self.assertFalse(result["ok"])
        self.assertIn("refused while writing", json.dumps(result["error"]["details"]))
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
        Project.objects.create(slug="example-site", name="Example site", public_url="https://pages.example.com")

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
        self.assertEqual(Project.objects.get(slug="example-site").public_url, "https://pages.example.com")
        self.assertEqual(result["summary"]["kept"], 1)

    def test_a_blank_public_url_is_filled(self):
        Project.objects.create(slug="example-site", name="Example site")

        run()

        self.assertEqual(Project.objects.get(slug="example-site").public_url, "https://www.example.com")


class CapabilityGateTests(TestCase):
    def test_it_requires_what_creating_both_requires(self):
        spec = command_spec("hq.import")
        creates = set(command_spec("project.create").required_capabilities) | set(
            command_spec("asset.create").required_capabilities
        )

        self.assertEqual(set(spec.required_capabilities), creates)
