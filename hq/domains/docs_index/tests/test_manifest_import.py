"""The manifest import page writes through the documentation.sync capability."""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from hq.platform.application.documentation import MAX_MANIFEST_ITEMS, SYNC
from hq.platform.application.security import Capability, Principal
from hq.platform.core.models import AuditLog

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
        self.client.force_login(get_user_model().objects.create_user("op"))

    def post(self, data, **extra):
        return self.client.post(
            reverse("docs_index:import"), {"manifest_file": _file(data), "update_existing": "on", **extra}, follow=True
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
        with patch("hq.domains.docs_index.views.web_principal", return_value=lacking):
            response = self.post([ENTRY])

        self.assertContains(response, "Nothing was imported")
        self.assertContains(response, Capability.SYNC_DOCUMENTATION.value)
        self.assertFalse(DocumentationRecord.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action=AuditLog.Action.DENIED, metadata__capability=SYNC).exists())

    def test_a_manifest_that_is_not_a_list_of_records_is_invalid_input(self):
        for data in ({"doc_id": "rb-example-service"}, ["not a record"]):
            with self.subTest(data=data):
                response = self.post(data)

                self.assertContains(response, "Nothing was imported")
                self.assertContains(response, "manifest")
        self.assertFalse(DocumentationRecord.objects.exists())

    def test_invalid_entries_are_named_and_nothing_is_imported(self):
        response = self.post([{**ENTRY, "doc_type": "not-a-type"}])

        self.assertContains(response, "Fix these entries first")
        self.assertFalse(DocumentationRecord.objects.exists())

    def test_the_safety_limit_keeps_its_own_words(self):
        response = self.post([ENTRY] * (MAX_MANIFEST_ITEMS + 1))

        # The page sets the figure apart, so the sentence is read around it.
        self.assertContains(response, "Nothing was imported: Manifest exceeds the")
        self.assertContains(response, "safety limit.")
        self.assertFalse(DocumentationRecord.objects.exists())
