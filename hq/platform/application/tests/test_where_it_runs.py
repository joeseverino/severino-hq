"""A project from the side of what serves it, and names in a note as links."""

from django.template import Context, Template
from django.test import TestCase

from hq.domains.control_plane.models import ManagedResource
from hq.domains.docs_index.models import DocumentationRecord

from ..entity_links import EntityLink, container_link
from ..projection import projection_scope
from ..where_it_runs import named_links, where_it_runs
from .test_relationships import estate


class WhereItRunsTests(TestCase):
    def test_a_published_address_names_its_service_machine_and_container(self):
        estate(1)

        with projection_scope():
            found = where_it_runs("https://s0.example.com/some/page")

        self.assertEqual(found.service.label, "s0.example.com")
        self.assertTrue(found.service.url.endswith("/s0.example.com/"))
        self.assertEqual(found.machine.label, "example-host-0")
        self.assertTrue(found.machine.url.endswith("/example-host-0/"))

    def test_an_address_no_service_answers_at_says_nothing(self):
        estate(1)

        with projection_scope():
            self.assertIsNone(where_it_runs("https://elsewhere.example.org/"))
            self.assertIsNone(where_it_runs(""))

    def test_the_partial_says_it_in_one_line_of_links(self):
        runs = where_it_runs.__globals__["RunsOn"](
            service=EntityLink("app.example.com", "/infrastructure/services/app.example.com/", kind="service"),
            machine=EntityLink("example-box", "/infrastructure/machines/example-box/", kind="machine"),
            container=container_link("example-box", "app"),
        )

        drawn = Template('{% include "partials/_runs_on.html" %}').render(Context({"runs_on": runs}))

        self.assertIn('Runs on <a href="/infrastructure/machines/example-box/"', drawn)
        self.assertIn('container <a href="/infrastructure/machines/example-box/#container-app"', drawn)
        self.assertIn('Served as <a href="/infrastructure/services/app.example.com/"', drawn)


class NamedLinksTests(TestCase):
    def setUp(self):
        ManagedResource.objects.create(key="example-box", kind="machine", spec={"name": "example-box"})
        DocumentationRecord.objects.create(doc_id="deploy-the-example", title="Deploy the example")

    def test_a_machine_and_a_document_a_note_names_are_links(self):
        with projection_scope():
            parts = named_links("Runs on example-box. See deploy the example, then restart.")

        self.assertEqual(
            [(part.label, part.kind) if isinstance(part, EntityLink) else part for part in parts],
            ["Runs on ", ("example-box", "machine"), ". See ", ("deploy the example", "document"), ", then restart."],
        )

    def test_a_name_inside_another_word_is_not_a_link(self):
        with projection_scope():
            self.assertEqual(named_links("the example-box-2 host"), ("the example-box-2 host",))

    def test_text_naming_nothing_stays_as_it_was(self):
        with projection_scope():
            self.assertEqual(named_links("Nothing known here."), ("Nothing known here.",))
            self.assertEqual(named_links(""), ())
