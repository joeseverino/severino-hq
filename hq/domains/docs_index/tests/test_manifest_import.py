"""The manifest import page writes through the documentation.sync capability."""

import json
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase
from django.urls import reverse
from pydantic import ValidationError

from hq.platform.application.capabilities import command_spec, validate_command
from hq.platform.application.documentation import MAX_MANIFEST_ITEMS, SYNC
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.application.security import Capability, Principal
from hq.platform.core.models import AuditLog
from hq.platform.core.testing import operator

from ..models import DocumentationRecord

ENTRY = {
    "doc_id": "rb-example-service",
    "title": "Example service",
    "doc_type": "runbook",
    "status": "active",
    "system": "Example service",
    "environment": "other",
    "sensitivity": "internal",
}


def _file(data) -> SimpleUploadedFile:
    return SimpleUploadedFile("manifest.json", json.dumps(data).encode(), content_type="application/json")


class ManifestImportViewTests(TestCase):
    def setUp(self):
        operator(self.client, "op")

    def post(self, data, **extra):
        return self.client.post(
            reverse("docs_index:import"),
            form_keyed({"manifest_file": _file(data), "update_existing": "on", **extra}),
            follow=True,
        )

    def test_a_valid_manifest_is_imported_and_audited_as_the_operator(self):
        response = self.post([ENTRY])

        self.assertRedirects(response, reverse("docs_index:list"))
        self.assertTrue(DocumentationRecord.objects.filter(doc_id=ENTRY["doc_id"]).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                action=AuditLog.Action.IMPORTED,
                metadata__operation=SYNC,
                metadata__interface="web",
                metadata__actor="op",
            ).exists()
        )

    def test_a_principal_that_may_not_sync_is_refused_and_recorded(self):
        lacking = Principal("op", "web", frozenset({Capability.READ}))
        with patch("hq.platform.application.web_writes.web_principal", return_value=lacking):
            response = self.post([ENTRY])

        self.assertContains(response, Capability.SYNC_DOCUMENTATION.value)
        self.assertFalse(DocumentationRecord.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SYNC).exists())

    def test_a_manifest_that_is_not_a_list_of_records_is_invalid_input(self):
        for data in ({"doc_id": "rb-example-service"}, ["not a record"]):
            with self.subTest(data=data):
                response = self.post(data)

                self.assertContains(response, "manifest")
        self.assertFalse(DocumentationRecord.objects.exists())

    def test_invalid_entries_are_named_and_nothing_is_imported(self):
        response = self.post([{**ENTRY, "doc_type": "not-a-type"}])

        self.assertContains(response, "Fix these entries first")
        self.assertFalse(DocumentationRecord.objects.exists())

    def test_the_safety_limit_keeps_its_own_words(self):
        response = self.post([ENTRY] * (MAX_MANIFEST_ITEMS + 1))

        # The page sets the figure apart, so the sentence is read around it.
        self.assertContains(response, "Manifest exceeds the")
        self.assertContains(response, "safety limit.")
        self.assertFalse(DocumentationRecord.objects.exists())


class ManifestTypeTests(SimpleTestCase):
    def test_an_entry_field_sent_in_a_json_type_its_schema_does_not_give_is_refused(self):
        spec = command_spec(SYNC)
        validate_command(spec, {"manifest": [ENTRY]})
        for field, sent in (("published", 0), ("published", "true"), ("published_at", 0), ("tags", "ops")):
            with self.subTest(field=field, sent=sent), self.assertRaises(ValidationError) as refused:
                validate_command(spec, {"manifest": [{**ENTRY, field: sent}]})

            self.assertEqual([error["loc"] for error in refused.exception.errors()], [("manifest", 0, field)])
