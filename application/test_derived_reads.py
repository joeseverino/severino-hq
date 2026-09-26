"""What the estate pages derive reaches the API, MCP, CLI and SDK from one registration."""

from __future__ import annotations

import json
import re
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from control_plane.models import ManagedResource, ProviderConnection, ProviderInventory
from control_plane.observations import OBSERVATIONS
from hq_api.tests import ISSUER, RESOURCE, _serving, _token
from hq_mcp import services as mcp_services
from hq_mcp.identity import reset_principal, set_principal
from hq_sdk import resources as sdk_resources

from .resources import describe_resources, get_resource, list_resource
from .security import AuthorizationError, Capability, Principal, mcp_principal
from .test_relationships import estate

READER = Principal("reader", "test", frozenset({Capability.READ}))
NOBODY = Principal("nobody", "test", frozenset({Capability.WRITE_PROJECTS}))

# Every derived read, and one identifier its detail answers to after ``estate``.
LISTS = {
    "estate": {},
    "action.items": {},
    "machines": {},
    "domains": {},
    "readings": {},
    "credentials": {},
    "search": {"query": "example"},
}
DETAILS = {
    "machines": "example-host-0",
    "domains": "example.com",
    "relationships": "service:s0.example.com",
    "readings": "cloudflare.pages_project",
    "credentials": "cloudflare_api",
}
SECRET_KEY_NAME = re.compile(r"secret|token|password|private|credential_value", re.I)
PLANTED = "planted-value-not-for-output"


def keys(value):
    """Every mapping key anywhere in a JSON-shaped value."""

    if isinstance(value, dict):
        for name, item in value.items():
            yield name
            yield from keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from keys(item)


def reset():
    ManagedResource.objects.all().delete()
    ProviderConnection.objects.all().delete()
    ProviderInventory.objects.all().delete()


class RegistrationTests(TestCase):
    def test_every_derived_read_is_a_described_resource(self):
        described = {item["name"]: item for item in describe_resources()["resources"]}

        for name in (*LISTS, *DETAILS):
            self.assertIn(name, described)
            self.assertEqual(described[name]["required_capabilities"], ["read"])
        for name in DETAILS:
            self.assertIsNotNone(described[name]["operations"]["get"])


class ApplicationTests(TestCase):
    def setUp(self):
        estate(2)

    def test_lists_answer_what_the_pages_read(self):
        machines = list_resource("machines", {}, principal=READER)
        domains = list_resource("domains", {}, principal=READER)

        self.assertEqual(
            [item["name"] for item in machines["items"]], ["example-host-0", "example-host-1"]
        )
        self.assertEqual(domains["items"][0]["name"], "example.com")
        self.assertEqual(
            sorted(item["hostname"] for item in domains["items"][0]["services"]),
            ["s0.example.com", "s1.example.com"],
        )

    def test_relationships_are_the_page_answer(self):
        from .relationships import relationships_for

        found = get_resource("relationships", "service:s0.example.com", principal=READER)
        page = relationships_for("service:s0.example.com", principal=READER)

        self.assertTrue(page.groups)
        self.assertEqual([group["phrase"] for group in found["groups"]], list(page.phrases()))
        self.assertEqual(
            [[item["entity"]["label"] for item in group["items"]] for group in found["groups"]],
            [[item.entity.label for item in group.items] for group in page.groups],
        )

    def test_an_unknown_node_is_not_found(self):
        from .resources import ResourceNotFound

        with self.assertRaises(ResourceNotFound):
            get_resource("relationships", "machine:absent", principal=READER)

    def test_search_puts_the_estate_first(self):
        found = list_resource("search", {"query": "example-host-0"}, principal=READER)

        self.assertEqual(found["items"][0]["kind"], "machine")
        self.assertEqual(found["items"][0]["name"], "example-host-0")

    def test_action_items_filter_as_the_page_does(self):
        everything = list_resource("action.items", {}, principal=READER)
        nothing = list_resource(
            "action.items", {"query": "no-such-words-anywhere"}, principal=READER
        )

        self.assertEqual(nothing["count"], 0)
        self.assertGreaterEqual(everything["count"], 0)


class AuthorizationTests(TestCase):
    def setUp(self):
        estate(1)

    def test_a_principal_without_read_is_refused_every_derived_read(self):
        for name, query in LISTS.items():
            with self.subTest(name), self.assertRaises(AuthorizationError):
                list_resource(name, query, principal=NOBODY)
        for name, identifier in DETAILS.items():
            with self.subTest(name), self.assertRaises(AuthorizationError):
                get_resource(name, identifier, principal=NOBODY)

    def test_the_mcp_tool_refuses_a_caller_without_read(self):
        bound = set_principal(NOBODY)
        self.addCleanup(reset_principal, bound)

        for name, query in LISTS.items():
            with self.subTest(name), self.assertRaises(AuthorizationError):
                mcp_services.list_resource(name, query)


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class ApiTests(TestCase):
    def setUp(self):
        estate(2)

    def get(self, path, scope="read"):
        with _serving():
            return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {_token(scope=scope)}")

    def test_every_list_and_detail_is_served(self):
        for name, query in LISTS.items():
            with self.subTest(name):
                suffix = "?" + "&".join(f"{k}={v}" for k, v in query.items()) if query else ""
                response = self.get(f"/api/v2/resources/{name}/{suffix}")
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(
                    response.json()["data"]["count"], len(response.json()["data"]["items"])
                )
        for name, identifier in DETAILS.items():
            with self.subTest(name):
                response = self.get(f"/api/v2/resources/{name}/{identifier}/")
                self.assertEqual(response.status_code, 200, response.content)

    def test_a_token_without_read_is_refused(self):
        for name in (*LISTS, *DETAILS):
            with self.subTest(name):
                path = (
                    f"/api/v2/resources/{name}/{DETAILS[name]}/"
                    if name in DETAILS
                    else f"/api/v2/resources/{name}/"
                )
                response = self.get(path, scope="example.write")
                self.assertEqual(response.status_code, 403)

    def test_an_unknown_identifier_is_404(self):
        self.assertEqual(self.get("/api/v2/resources/machines/absent/").status_code, 404)
        self.assertEqual(self.get("/api/v2/resources/readings/absent.kind/").status_code, 404)

    def test_unknown_filters_are_refused(self):
        self.assertEqual(self.get("/api/v2/resources/machines/?limti=1").status_code, 400)


class McpCliAndSdkTests(TestCase):
    def setUp(self):
        estate(2)
        bound = set_principal(mcp_principal())
        self.addCleanup(reset_principal, bound)

    def test_the_generic_mcp_tools_serve_every_derived_read(self):
        for name, query in LISTS.items():
            with self.subTest(name):
                self.assertIn("items", mcp_services.list_resource(name, query))
        for name, identifier in DETAILS.items():
            with self.subTest(name):
                self.assertTrue(mcp_services.get_resource(name, identifier))

    def test_the_cli_reaches_them_through_the_same_tool(self):
        output = StringIO()
        request = {"tool": "get_resource",
                   "arguments": {"name": "machines", "identifier": "example-host-1"}}
        with mock.patch("sys.stdin", StringIO(json.dumps(request))):
            call_command("hq_call", stdout=output)

        self.assertEqual(json.loads(output.getvalue())["name"], "example-host-1")

    def test_the_sdk_reads_them_through_the_same_functions(self):
        self.assertIs(sdk_resources.list_resource, list_resource)
        self.assertEqual(
            sdk_resources.get_resource("domains", "example.com", principal=READER)["name"],
            "example.com",
        )


class NoSecretTests(TestCase):
    """Stored readings leave only through their schema; nothing secret is named."""

    def setUp(self):
        estate(2)
        row = ProviderInventory.objects.get(kind="cloudflare.pages_project")
        row.records = [{**row.records[0], "api_token": PLANTED, "env_vars": {"KEY": PLANTED}}]
        row.save()

    def test_a_reading_returns_only_its_schema_fields(self):
        found = get_resource("readings", "cloudflare.pages_project", principal=READER)
        fields = set(OBSERVATIONS["cloudflare.pages_project"].record.model_fields)

        self.assertTrue(found["items"])
        for record in found["items"]:
            self.assertLessEqual(set(record), fields)
        self.assertNotIn(PLANTED, json.dumps(found))

    def test_relationships_return_only_schema_fields(self):
        found = get_resource("relationships", "zone:example.com", principal=READER)

        self.assertTrue(found["readings"])
        for reading in found["readings"]:
            fields = set(OBSERVATIONS[reading["kind"]].record.model_fields)
            for record in reading["records"]:
                self.assertLessEqual(set(record), fields)
        self.assertNotIn(PLANTED, json.dumps(found))

    def test_no_derived_read_names_a_secret_looking_field(self):
        answers = [list_resource(name, query, principal=READER) for name, query in LISTS.items()]
        answers += [
            get_resource(name, identifier, principal=READER)
            for name, identifier in DETAILS.items()
        ]
        for answer in answers:
            named = {name for name in keys(answer) if SECRET_KEY_NAME.search(str(name))}
            self.assertEqual(named, set())
            self.assertNotIn(PLANTED, json.dumps(answer, default=str))


class QueryBudgetTests(TestCase):
    """A list costs the same number of queries however large the estate is."""

    def cost(self, size, call):
        reset()
        estate(size)
        with CaptureQueriesContext(connection) as captured:
            call()
        return len(captured)

    def assert_flat(self, name, call):
        small, large = self.cost(2, call), self.cost(10, call)
        self.assertLessEqual(large, small, f"{name} grows with the estate ({small} then {large})")

    def test_lists(self):
        for name, query in LISTS.items():
            with self.subTest(name):
                self.assert_flat(name, lambda: list_resource(name, query, principal=READER))

    def test_details(self):
        for name, identifier in DETAILS.items():
            with self.subTest(name):
                self.assert_flat(
                    name, lambda: get_resource(name, identifier, principal=READER)
                )


class ServedMachineReadTests(TestCase):
    """The machines resource places HQ the way the machine page does, inside a
    projection seeded with where the request arrived."""

    def test_the_address_a_request_arrived_on_places_hq(self):
        from types import SimpleNamespace

        from .hq_self import serving
        from .projection import projection_scope
        from .test_hq_self import SITE, declare, own

        declare("example-host", "192.0.2.44")
        request = SimpleNamespace(scope={"server": ("192.0.2.44", 443)})
        with SITE, own():
            unseeded = list_resource("machines", {}, principal=READER)
            with projection_scope(seed=serving(request)):
                seeded = list_resource("machines", {}, principal=READER)
                detail = get_resource("machines", "example-host", principal=READER)

        self.assertFalse(unseeded["items"][0]["runs_hq"])
        self.assertTrue(seeded["items"][0]["runs_hq"])
        self.assertTrue(detail["runs_hq"])


class OneDomainListTests(TestCase):
    """The API, the estate count and the domains page list the same domains."""

    def setUp(self):
        ManagedResource.objects.create(
            key="example-com",
            kind="cloudflare.zone",
            enabled=True,
            spec={"zone": "example.com", "connection_ref": "cf"},
        )
        # A record in a zone nothing declares or sweeps.
        ManagedResource.objects.create(
            key="www-example-org",
            kind="cloudflare.dns_record",
            enabled=True,
            spec={
                "zone": "example.org",
                "name": "www.example.org",
                "record_type": "A",
                "content": "192.0.2.10",
                "proxied": False,
                "ttl": 1,
            },
        )

    def test_a_zone_known_only_by_its_records_is_listed_everywhere(self):
        from .estate import estate_reading
        from .projection import projection_scope
        from .zones import zone_catalog

        api = [item["name"] for item in list_resource("domains", {}, principal=READER)["items"]]
        with projection_scope():
            counted = list(estate_reading().domains)
        page = [zone.zone for zone in zone_catalog()]

        self.assertEqual(api, ["example.com", "example.org"])
        self.assertEqual(counted, api)
        self.assertEqual(page, api)

    def test_a_domain_detail_carries_its_records_and_connection(self):
        found = get_resource("domains", "example.org", principal=READER)

        self.assertEqual([record["name"] for record in found["records"]], ["www.example.org"])
        self.assertEqual(found["records"][0]["declaration"], "www-example-org")
        self.assertIn("reachable", found)
        self.assertIsInstance(found["insights"], list)
        self.assertEqual(get_resource("domains", "example.com", principal=READER)["connection_ref"], "cf")
