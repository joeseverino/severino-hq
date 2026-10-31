"""The content and documentation lists, read as any resource is."""

from django.test import TestCase

from hq.domains.content.models import ContentItem
from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.application.resources import list_resource
from hq.platform.application.security import AuthorizationError, Capability, Principal

READER = Principal("reader", "api", frozenset({Capability.READ}))
NOBODY = Principal("nobody", "api", frozenset())


class RecordListTests(TestCase):
    def test_a_caller_who_may_not_read_is_refused(self):
        for resource in ("content", "documentation"):
            with self.subTest(resource=resource), self.assertRaises(AuthorizationError):
                list_resource(resource, {}, principal=NOBODY)

    def test_documentation_lists_only_what_any_reader_may_see(self):
        for sensitivity in DocumentationRecord.Sensitivity.values:
            DocumentationRecord.objects.create(
                doc_id=f"example-{sensitivity}", title=f"Example {sensitivity}", sensitivity=sensitivity
            )

        listed = {item["doc_id"] for item in list_resource("documentation", {}, principal=READER)["items"]}

        self.assertEqual(listed, {"example-public", "example-internal"})

    def test_content_lists_its_records(self):
        ContentItem.objects.create(title="Example writeup", slug="example-writeup")

        (item,) = list_resource("content", {}, principal=READER)["items"]

        self.assertEqual((item["slug"], item["title"]), ("example-writeup", "Example writeup"))
