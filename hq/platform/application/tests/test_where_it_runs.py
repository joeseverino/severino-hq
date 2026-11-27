"""A project from the side of what serves it, and names in a note as links."""

from django.test import TestCase

from hq.domains.docs_index.models import DocumentationRecord
from hq.platform.core.testing import declare

from ..entity_links import EntityLink
from ..projection import projection_scope
from ..where_it_runs import named_links


class NamedLinksTests(TestCase):
    def setUp(self):
        declare("example-box", "machine", {"name": "example-box"})
        DocumentationRecord.objects.create(doc_id="deploy-the-example", title="Deploy the example")

    def test_a_machine_and_a_document_a_note_names_are_links(self):
        with projection_scope():
            parts = named_links("Runs on example-box. See deploy the example, then restart.")

        self.assertEqual(
            [(part.label, part.kind) if isinstance(part, EntityLink) else part for part in parts],
            ["Runs on ", ("example-box", "machine"), ". See ", ("deploy the example", "document"), ", then restart."],
        )
