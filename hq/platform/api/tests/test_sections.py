"""Each operation of the API document stands in the section of the domain that serves it."""

import re
from dataclasses import replace

from django.test import SimpleTestCase

from hq.platform.application.integration_specs import CapabilitySpec, ResourceSpec
from hq.platform.application.outbound_work import OutboundWork
from hq.platform.application.plugin_testing import installed, sibling
from hq.platform.application.plugins import NavigationGroup, NavigationItem
from hq.platform.application.resources import TopQuery
from hq.platform.application.security import Capability
from hq_sdk.capabilities import StrictCommand

from .. import openapi, urls


class _Nothing(StrictCommand):
    pass


class _ExampleQuery(TopQuery):
    pass


def _rows(*, limit: int = 50) -> dict:
    return {"items": [], "count": 0}


def _run(_command, *, principal) -> dict:
    return {"ok": True}


def _work(progress, *, subject, principal) -> None:
    return None


def _trails():
    """An extension heading a group named for it, with a command, a read and outbound work of its own."""

    manifest, integration = sibling(
        identifier="example.trails",
        name="Trails",
        description="Walks and the paths they take.",
        navigation=(NavigationItem("Trails", "example_trails:home", "example_trails", 40, "Trails"),),
        navigation_groups=(NavigationGroup("Trails", "Time spent outside, and where."),),
        operator_capabilities=("example.trails.write",),
    )
    command = CapabilitySpec(
        "example.trails.log", "Log a walk.", "remote_write", "example.trails.write", _Nothing, _run, label="Log a walk"
    )
    rows = ResourceSpec("example.trails.walks", "Walks", "Every walk logged.", Capability.READ, _rows, _ExampleQuery)
    work = OutboundWork(
        name="example.trails.sync",
        label="Sync walks",
        summary="Read the walks again from the watch.",
        required_capability="example.trails.write",
        run=_work,
    )
    return manifest, replace(
        integration, capabilities=lambda: (command,), resources=lambda: (rows,), outbound=lambda: (work,)
    )


def _summits():
    """A second extension in the same group, with a read of its own."""

    manifest, integration = sibling(
        identifier="example.summits",
        name="Summits",
        description="The summits reached.",
        navigation=(NavigationItem("Summits", "example_summits:home", "example_summits", 41, "Trails"),),
    )
    rows = ResourceSpec(
        "example.summits.reached", "Reached", "Every summit reached.", Capability.READ, _rows, _ExampleQuery
    )
    return manifest, replace(integration, resources=lambda: (rows,))


def _operations(document):
    for path, item in document["paths"].items():
        for method, operation in item.items():
            yield f"{method.upper()} {path}", operation


def _own_routes() -> set[str]:
    """The paths of the API's own routes: those that name no page of one domain."""

    return {
        openapi.path_template(route)
        for route, pattern in openapi.routes()
        if not openapi._view(pattern).__hq_page__ and id(pattern) in {id(item) for item in urls.urlpatterns}
    }


def _lead(text: str) -> str:
    return re.split(r"[:,.;]", text, maxsplit=1)[0].strip().lower()


class SectionsTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()

    def _sections_hold(self, document):
        sections = {tag["name"] for tag in document["tags"]}
        own = _own_routes()
        for where, operation in _operations(document):
            with self.subTest(where):
                self.assertEqual(len(operation.get("tags", [])), 1, "every operation stands in one section")
                (section,) = operation["tags"]
                self.assertIn(section, sections)
                path = where.partition(" ")[2]
                self.assertEqual(section == openapi.API_TAG, path in own, "only the API's own routes are its")

    def test_every_operation_stands_in_the_section_of_its_domain_and_the_api_holds_only_its_own(self):
        self._sections_hold(self.document)

    def test_a_core_command_stands_in_the_section_of_the_domain_that_declares_it(self):
        lookup = self.document["paths"]["/api/v2/capabilities/lookup.name.read/"]["post"]
        self.assertEqual(lookup["tags"], ["hq.tools"])

    def test_a_route_serving_one_domain_stands_in_its_section(self):
        self.assertEqual(self.document["paths"]["/api/v2/topology/"]["get"]["tags"], ["hq.topology"])

    def test_an_extensions_command_read_and_work_stand_in_its_section(self):
        with installed(_trails(), _summits()):
            document = openapi.document()
        paths = document["paths"]
        for where in (
            paths["/api/v2/capabilities/example.trails.log/"]["post"],
            paths["/api/v2/capabilities/example.trails.sync/"]["post"],
            paths["/api/v2/resources/example.trails.walks/"]["get"],
        ):
            self.assertEqual(where["tags"], ["example.trails"])
        self._sections_hold(document)
        tags = {tag["name"]: tag for tag in document["tags"]}
        # The domain named as its group heads it, and its sibling stands under it.
        self.assertNotIn("parent", tags["example.trails"])
        self.assertEqual(tags["example.trails"]["description"], "Time spent outside, and where.")
        self.assertEqual(tags["example.summits"]["parent"], "example.trails")

    def test_no_section_or_operation_is_named_as_the_section_it_stands_in(self):
        with installed(_trails(), _summits()):
            composed = openapi.document()
        for document in (self.document, composed):
            tags = {tag["name"]: tag for tag in document["tags"]}
            for tag in tags.values():
                if "parent" in tag:
                    with self.subTest(tag["name"]):
                        self.assertNotEqual(tag["summary"], tags[tag["parent"]]["summary"])
            for where, operation in _operations(document):
                with self.subTest(where):
                    self.assertNotEqual(operation["summary"], tags[operation["tags"][0]]["summary"])

    def test_no_operation_says_again_what_its_section_says(self):
        sections = {tag["name"]: tag.get("description", "") for tag in self.document["tags"]}
        for where, operation in _operations(self.document):
            said, section = operation.get("description", ""), sections[operation["tags"][0]]
            with self.subTest(where):
                self.assertNotEqual(_lead(said), _lead(section))
