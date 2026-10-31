"""MCP answers fit a client: the capability catalog narrows, and a cut-off list says so."""

from asgiref.sync import async_to_sync
from django.test import TestCase
from pydantic import ValidationError

from hq.domains.control_plane.models import ManagedResource
from hq.platform.application.capabilities import InvalidCapabilityQuery
from hq.platform.application.integration_specs import ResourceSpec
from hq.platform.application.projection import MAX_PAGE_SIZE
from hq.platform.application.resources import BoundedQuery, InvalidResourceInput, _page
from hq.platform.application.security import mcp_principal

from .. import services
from ..identity import reset_principal, set_principal
from ..server import mcp


def call(tool: str, **arguments):
    return async_to_sync(mcp.tools.get(tool).call)(arguments)


class CapabilityCatalogNarrowsTests(TestCase):
    def setUp(self):
        self.addCleanup(reset_principal, set_principal(mcp_principal()))

    def test_a_name_keeps_the_capabilities_under_it(self):
        everything = {item["name"] for item in call("describe_capabilities")["capabilities"]}
        narrowed = {item["name"] for item in call("describe_capabilities", name="infrastructure")["capabilities"]}

        self.assertIn("infrastructure.reconcile", narrowed)
        self.assertEqual(narrowed, {name for name in everything if name.startswith("infrastructure.")})
        self.assertLess(len(narrowed), len(everything))

    def test_a_name_matches_whole_segments_only(self):
        self.assertEqual(call("describe_capabilities", name="infra")["capabilities"], [])

    def test_a_resource_keeps_the_capabilities_that_act_on_it(self):
        narrowed = call("describe_capabilities", resource="infrastructure.resources")["capabilities"]

        self.assertIn("infrastructure.resource.update", [item["name"] for item in narrowed])
        self.assertEqual({item["resource"] for item in narrowed}, {"infrastructure.resources"})

    def test_input_that_names_nothing_is_refused(self):
        for arguments in ({"name": "Infrastructure!"}, {"resource": "example.nothing"}):
            with self.subTest(arguments=arguments), self.assertRaises(InvalidCapabilityQuery):
                call("describe_capabilities", **arguments)
        with self.assertRaises(ValidationError):
            call("describe_capabilities", name=5)


class CutOffListSaysSoTests(TestCase):
    def setUp(self):
        self.addCleanup(reset_principal, set_principal(mcp_principal()))
        for index in range(3):
            ManagedResource.objects.create(
                key=f"example-proxy-{index}",
                kind="npm.proxy_host",
                spec={"domain_names": [f"app{index}.example.com"], "forward_host": "192.0.2.10"},
            )

    def test_a_full_page_says_it_was_cut_off_and_how_to_ask_for_the_rest(self):
        page = services.list_resource("infrastructure.resources", {"limit": 2})

        self.assertEqual(page["count"], 2)
        self.assertEqual(page["total"], 3)
        self.assertTrue(page["truncated"])
        self.assertIn(f"limit up to {MAX_PAGE_SIZE}", page["next"])
        self.assertIn("kind", page["next"])

    def test_a_page_holding_every_match_says_so(self):
        page = services.list_resource("infrastructure.resources", {"limit": 5})

        self.assertEqual((page["count"], page["total"], page["truncated"], page["next"]), (3, 3, False, ""))

    def test_a_field_mask_keeps_the_notice(self):
        page = services.list_resource("infrastructure.resources", {"limit": 2}, fields=["key"])

        self.assertTrue(page["truncated"])
        self.assertEqual(page["total"], 3)

    def test_an_invalid_filter_is_refused(self):
        for filters in ({"limit": 0}, {"kinds": "Not A Kind"}, {"page": 2}):
            with self.subTest(filters=filters), self.assertRaises(InvalidResourceInput):
                services.list_resource("infrastructure.resources", filters)

    def test_a_full_page_from_a_list_that_does_not_count_is_read_as_cut_off(self):
        spec = ResourceSpec("example.things", "Things", "Things.", "read", lambda **_: {}, BoundedQuery)
        full = {"items": [{}, {}], "count": 2}

        self.assertTrue(_page(spec, BoundedQuery(limit=2), full)["truncated"])
        self.assertIsNone(_page(spec, BoundedQuery(limit=2), full)["total"])
        self.assertFalse(_page(spec, BoundedQuery(limit=3), full)["truncated"])
