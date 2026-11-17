"""Every record domain's save answers the record in the shape the document
gives it, on the showcase estate: the shape is read off the resource's
declared record type and the answer comes from its serializer, so a field
one writes and the other lacks fails here (the client holds each answer to
the document)."""

from django.test import TestCase

from hq.platform.application.domains import host_records, load
from hq.platform.application.tests.host_only import host_only
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .doors import API, Caller, at_the_door, each_door


class SavesAnswerTheirRecordTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def setUp(self):
        at_the_door(self)

    def test_each_record_domains_update_answers_the_record_as_the_document_says(self):
        domains = list(host_records())
        writer = Caller("example-writer", " ".join(["read", *(records.write for records in domains)]))
        saved = set()
        with host_only():
            for records in domains:
                first = load(records.model).objects.order_by("pk").first()
                self.assertIsNotNone(first, f"the showcase holds no {records.noun}")
                for door in each_door(self, writer, doors=(API,)):
                    with door:
                        answer = door.capability(
                            f"{records.noun}.update",
                            {"idempotency_key": "example-save"},
                            target=getattr(first, records.lookup),
                        )

                        self.assertTrue(answer.get("ok"), (records.noun, answer))
                        self.assertIn(records.noun, answer, (records.noun, answer))
                        saved.add(records.noun)

        self.assertIn("content", saved)
