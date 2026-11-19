"""The OpenAPI document is complete, and what the API really answers.

Complete means derived: every route appears with the methods it serves, and
each capability and resource operation is written from its registry entry by
``openapi._Builder``. What the API really answers is held by
``ContractClient`` on every TransportTests request; the tests below cover the
document's own route and the contract client's refusals.
"""

import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from jsonschema import ValidationError

from hq.platform.application.capabilities import capability_registry
from hq.platform.application.domains import all_domains
from hq.platform.application.resources import resource_registry
from hq.platform.application.security import Capability
from hq.platform.core.models import AuditLog
from hq.platform.core.test_runner import no_default_role

from .. import openapi, views
from ..components import Components
from ..tags import Tags
from ..testing import ContractClient
from .tokens import ISSUER, OTHER_KEY, RESOURCE, _serving, _token

# A list that matched nothing, with every field the collection contract requires.
EMPTY_PAGE = {"items": [], "count": 0, "total": 0, "truncated": False, "next": None}


def _operations(value: dict) -> list[tuple[str, str, dict]]:
    return [(path, method, operation) for path, item in value["paths"].items() for method, operation in item.items()]


class DocumentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()

    def test_every_route_appears_with_its_methods(self):
        for route, pattern in openapi.routes():
            path = openapi.path_template(route)
            with self.subTest(route=route):
                self.assertIn(path, self.document["paths"])
                self.assertEqual(
                    set(self.document["paths"][path]),
                    {method.lower() for method in pattern.callback.__hq_methods__},
                )

    def test_tags_are_the_nav_tree(self):
        tags = {tag["name"]: tag for tag in self.document["tags"]}
        for _, _, operation in _operations(self.document):
            self.assertLessEqual(set(operation["tags"]), set(tags))
        for tag in tags.values():
            if "parent" in tag:
                self.assertIn(tag["parent"], tags)
                self.assertEqual(tags[tag["parent"]]["kind"], "nav")
        projects = self.document["paths"]["/api/v2/resources/projects/"]["get"]
        self.assertEqual(projects["tags"], ["hq.projects"])
        self.assertEqual(tags["hq.docs"]["parent"], "hq.projects")
        self.assertEqual(tags["hq.content"]["parent"], "nav.web")

    def test_the_apis_own_section_leads_and_the_rest_stand_in_the_order_of_the_bar(self):
        """The group holding the API's own domain first, that domain first in
        it, then the bar's order: a group where its first domain is, its
        domains after it, the rest in place."""

        domains = {domain.id: domain for domain in all_domains()}
        tags = self.document["tags"]
        parent = {tag["name"]: tag.get("parent") for tag in tags}
        placed = [tag["name"] for tag in tags if tag["name"] in domains]
        self.assertEqual(placed[0], openapi.API_TAG)
        self.assertEqual(tags[0]["name"], parent[openapi.API_TAG])

        def bar(name):
            return domains[name].bar_order, domains[name].label

        leading = [name for name in placed if parent[name] == parent[openapi.API_TAG]]
        self.assertEqual(placed[: len(leading)], leading)
        self.assertEqual(leading[1:], sorted(leading[1:], key=bar))
        # A domain heading its group stands where the group does, ahead of the domains under it.
        rest = [name for name in placed[len(leading) :] if name not in parent.values()]
        self.assertEqual(rest, sorted(rest, key=bar))
        seen: list[str] = []
        for tag in tags:
            if "parent" in tag:
                self.assertIn(tag["parent"], seen)
            seen.append(tag["name"])
        parents = [tag["parent"] for tag in tags if "parent" in tag]
        groups = [tag["name"] for tag in tags if tag["name"] in parents]
        self.assertEqual(groups, list(dict.fromkeys(parents)))

    def test_the_apis_own_routes_stand_under_the_api_where_the_bar_has_it(self):
        tags = {tag["name"]: tag for tag in self.document["tags"]}
        api = next(domain for domain in all_domains() if domain.id == openapi.API_TAG)
        group = next(item.group for item in api.navigation)
        self.assertEqual(tags[openapi.API_TAG]["parent"], f"nav.{group.lower()}")
        root = self.document["paths"]["/api/v2/"]["get"]
        self.assertEqual(root["tags"][0], openapi.API_TAG)

    def test_no_section_repeats_the_documents_description(self):
        tags = self.document["tags"]
        said = self.document["info"]["description"]
        for tag in tags:
            with self.subTest(tag=tag["name"]):
                self.assertFalse(tag.get("description") and tag["description"] in said)

    def test_each_resource_and_capability_is_tagged_with_the_domain_it_belongs_to(self):
        """Its declaring domain, else the domain of its page, else of its subject; else none."""

        tags = Tags(resource_registry(), capability_registry())
        self.assertEqual(tags.of_resource("projects"), "hq.projects")
        self.assertEqual(tags.of_resource("services"), "hq.services")
        self.assertEqual(tags.of_resource("search"), "hq.search")
        self.assertEqual(tags.of_capability("project.create"), "hq.projects")
        self.assertEqual(tags.of_capability("infrastructure.zone.adopt"), "hq.zones")
        # Held only in the browser, so never in the document: no domain's to place.
        self.assertIsNone(tags.of_capability("approval.approve"))
        declaring = next(domain for domain in all_domains() if domain.integration.capabilities)
        declared = next(iter(declaring.integration.capabilities())).name
        self.assertEqual(tags.of_capability(declared), declaring.id)

    def test_every_operation_has_a_name_short_enough_for_a_sidebar(self):
        """A summary is a title; the sentence about it is the description."""

        for path, item in self.document["paths"].items():
            for method, operation in item.items():
                with self.subTest(path=path, method=method):
                    self.assertLessEqual(len(operation["summary"]), 48)
                    self.assertFalse(operation["summary"].endswith("."))

    def test_local_schema_definitions_are_hoisted_under_their_own_names(self):
        components = Components()
        hoisted = components.hoist(
            {
                "type": "object",
                "$defs": {"Record": {"type": "object"}},
                "properties": {"r": {"$ref": "#/$defs/Record"}},
            }
        )
        self.assertNotIn("$defs", hoisted)
        self.assertEqual(hoisted["properties"]["r"]["$ref"], "#/components/schemas/Record")
        self.assertIn("Record", components.schemas)

    def test_one_name_is_one_definition_wherever_definitions_are_gathered(self):
        """The document's components and a command's request body, an extension's included."""

        envelope = views.ENVELOPE_SCHEMA["$defs"]
        name = next(iter(envelope))
        command = {"properties": {"at": {"$ref": f"#/$defs/{name}"}}, "$defs": {name: envelope[name]}}
        body = views.request_schema({"name": "example.thing.touch", "input_schema": command, "target": None})
        self.assertEqual(
            (body["$defs"], body["properties"]["payload"]["properties"]["at"]), (envelope, command["properties"]["at"])
        )
        components = Components()
        components.hoist({"$defs": envelope})
        for gather in (
            lambda clash: components.hoist({"$defs": clash}),
            lambda clash: views.request_schema(
                {"name": "example.thing.touch", "input_schema": {"$defs": clash}, "target": None}
            ),
        ):
            gather(envelope)
            with self.subTest(gather), self.assertRaisesRegex(RuntimeError, name):
                gather({name: {"type": "integer"}})

    def test_a_parameterized_route_without_an_expansion_fails_the_build(self):
        with patch.dict(openapi.EXPANSIONS, clear=True), self.assertRaises(openapi.OpenAPIError):
            openapi.document()


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class ServedDocumentTests(TestCase):
    client_class = ContractClient

    def test_the_served_document_is_the_derived_one(self):
        with _serving():
            response = self.client.get(reverse("hq_api:openapi"), HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), openapi.document())
        self.assertEqual(response["Cache-Control"], "private, no-store")

    def test_an_anonymous_caller_is_refused(self):
        response = self.client.get(reverse("hq_api:openapi"))
        self.assertEqual(response.status_code, 401)

    def _sign_in(self):
        self.client.force_login(get_user_model().objects.create_user("operator"))

    def test_the_signed_in_operator_reads_it(self):
        self._sign_in()
        response = self.client.get(reverse("hq_api:openapi"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), openapi.document())

    def test_a_signed_in_person_with_no_role_is_refused_it_as_a_page_is(self):
        with no_default_role():
            self.client.force_login(get_user_model().objects.create_user("example-nobody"))

        response = self.client.get(reverse("hq_api:openapi"))

        self.assertEqual(response.status_code, 403)
        denial = AuditLog.objects.filter(action=AuditLog.Action.DENIED).get()
        self.assertEqual(denial.metadata["capability"], Capability.READ)

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_the_operator_reads_it_where_the_token_surface_is_off(self):
        self._sign_in()
        self.assertEqual(self.client.get(reverse("hq_api:openapi")).status_code, 200)
        # Nothing else follows the session.
        self.assertEqual(self.client.get(reverse("hq_api:root")).status_code, 503)

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_an_anonymous_caller_still_gets_the_fail_closed_answer(self):
        self.assertEqual(self.client.get(reverse("hq_api:openapi")).status_code, 503)

    def test_a_session_opens_nothing_else(self):
        self._sign_in()
        self.assertEqual(self.client.get(reverse("hq_api:root")).status_code, 401)

    def test_a_presented_token_is_judged_even_with_a_session(self):
        self._sign_in()
        with _serving():
            response = self.client.get(
                reverse("hq_api:openapi"),
                HTTP_AUTHORIZATION=f"Bearer {_token(key=OTHER_KEY)}",
            )
        self.assertEqual(response.status_code, 401)

    def test_the_root_links_it(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.json()["data"]["links"]["openapi"], reverse("hq_api:openapi"))


class ContractClientTests(SimpleTestCase):
    """The contract client refuses what the document does not describe."""

    def setUp(self):
        self.client = ContractClient()

    def _answer(self, url, status, body, method="GET"):
        response = HttpResponse(json.dumps(body), status=status, content_type="application/json")
        self.client._conform(method.lower(), url, "", response)

    def test_an_undocumented_status_fails(self):
        with self.assertRaisesRegex(AssertionError, "does not list"):
            self._answer("/api/v2/", 418, {"ok": False, "error": {"code": "x", "message": "y"}})

    def test_an_undocumented_path_fails(self):
        with self.assertRaisesRegex(AssertionError, "not in the OpenAPI document"):
            self._answer("/api/v2/nowhere/", 200, {})

    def test_a_body_off_its_schema_fails(self):
        with self.assertRaises(ValidationError):
            self._answer("/api/v2/", 200, {"ok": True, "data": {"service": "severino-hq"}})

    def test_a_whole_answer_missing_a_field_its_record_promises_fails(self):
        saved = {"created": True, "message": "Saved.", "asset": {}}
        with self.assertRaisesRegex(ValidationError, "'slug' is a required property"):
            self._answer("/api/v2/capabilities/asset.create/", 200, {"ok": True, "data": saved}, method="POST")

    def test_resource_collections_accept_projection_metadata(self):
        self._answer(
            "/api/v2/resources/projects/",
            200,
            {"ok": True, "data": {**EMPTY_PAGE, "filters": {"query": "example"}}},
        )

    def test_resource_collection_metadata_does_not_relax_items_or_count(self):
        for data in (
            {"items": "wrong", "count": 0},
            {"items": [], "count": "wrong"},
            {"items": []},
            {"count": 0},
        ):
            with self.subTest(data=data), self.assertRaises(ValidationError):
                self._answer("/api/v2/resources/projects/", 200, {"ok": True, "data": data})

    def test_an_error_off_the_envelope_fails(self):
        with self.assertRaises(ValidationError):
            self._answer("/api/v2/", 401, {"ok": False, "detail": "no"})

    def test_a_concrete_path_is_matched_before_its_template(self):
        contract = self.client.contract
        self.assertEqual(contract.path("/api/v2/capabilities/project.create/"), "/api/v2/capabilities/project.create/")
        self.assertEqual(contract.path("/api/v2/capabilities/nope/"), "/api/v2/capabilities/{name}/")
