"""The OpenAPI document is valid, complete, and what the API really answers.

Complete means derived: every route, capability, resource operation and domain
appears without anyone listing it (application/tests/test_domain_declaration.py
proves a new domain's commands and resource arrive with no edit here). What the
API really answers is held by ``ContractClient`` on every TransportTests
request; the tests below cover the document's own route and the contract
client's refusals.
"""

from __future__ import annotations

import importlib.util
import json
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from jsonschema import ValidationError

from hq.platform.application.capabilities import capability_registry, describe_capabilities
from hq.platform.application.resources import resource_registry

from . import openapi, views
from .tests import _OTHER_KEY as OTHER_KEY, ISSUER, RESOURCE, _serving, _token
from .testing import Contract, ContractClient

VALIDATOR = importlib.util.find_spec("openapi_spec_validator") is not None


def _operations(value: dict) -> list[tuple[str, str, dict]]:
    return [
        (path, method, operation)
        for path, item in value["paths"].items()
        for method, operation in item.items()
    ]


class DocumentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()

    @skipUnless(VALIDATOR, "openapi-spec-validator is a development tool, not in the image")
    def test_it_is_a_valid_openapi_3_2_document(self):
        from openapi_spec_validator import validate
        from openapi_spec_validator.validation import OpenAPIV32SpecValidator

        self.assertTrue(self.document["openapi"].startswith("3.2."))
        validate(self.document, cls=OpenAPIV32SpecValidator)

    def test_it_names_its_served_url(self):
        self.assertEqual(self.document["$self"], reverse("hq_api:openapi"))

    def test_every_route_appears_with_its_methods(self):
        for route, pattern in openapi.routes():
            path = openapi.path_template(route)
            with self.subTest(route=route):
                self.assertIn(path, self.document["paths"])
                self.assertEqual(
                    set(self.document["paths"][path]),
                    {method.lower() for method in pattern.callback.__hq_methods__},
                )

    def test_every_capability_appears(self):
        found = {
            operation["x-hq-capability"]
            for _, _, operation in _operations(self.document)
            if "x-hq-capability" in operation
        }
        self.assertEqual(found, set(capability_registry()))

    def test_every_resource_operation_appears(self):
        listed, detailed = set(), set()
        for path, _, operation in _operations(self.document):
            if name := operation.get("x-hq-resource"):
                (detailed if path.endswith("{identifier}/") else listed).add(name)
        registry = resource_registry()
        self.assertEqual(listed, {name for name, spec in registry.items() if spec.list_handler})
        self.assertEqual(detailed, {name for name, spec in registry.items() if spec.identifier})

    def test_list_queries_emit_each_field_and_preserve_the_strict_schema(self):
        from hq.platform.application.resources import describe_resources

        resources = {spec["name"]: spec for spec in describe_resources()["resources"]}
        for _, _, operation in _operations(self.document):
            name = operation.get("x-hq-resource")
            if not name or "x-hq-identifier" in operation:
                continue
            expected = openapi._Components().hoist(
                resources[name]["operations"]["list"]["query_schema"],
                f"{openapi._pascal(name)}Query",
            )
            self.assertEqual(operation["x-hq-query-schema"], expected)
            parameters = {parameter["name"]: parameter for parameter in operation["parameters"]}
            self.assertEqual(set(parameters), set(expected.get("properties", {})))
            for field, schema in expected.get("properties", {}).items():
                parameter = parameters[field]
                self.assertEqual(parameter["in"], "query")
                self.assertEqual(parameter["style"], "form")
                self.assertIs(parameter["explode"], True)
                self.assertEqual(parameter["required"], field in expected.get("required", []))
                self.assertEqual(parameter["schema"], schema)
                self.assertEqual(parameter.get("description"), schema.get("description"))
                self.assertNotIn("content", parameter)

    def test_detail_operations_name_the_canonical_identifier(self):
        registry = resource_registry()
        for _, _, operation in _operations(self.document):
            if identifier := operation.get("x-hq-identifier"):
                self.assertEqual(identifier, registry[operation["x-hq-resource"]].identifier)
        detailed = {
            operation["x-hq-resource"]
            for _, _, operation in _operations(self.document)
            if "x-hq-identifier" in operation
        }
        self.assertEqual(detailed, {name for name, spec in registry.items() if spec.identifier})

    def test_every_write_requires_a_retry_key(self):
        for path, _, operation in _operations(self.document):
            if operation.get("x-hq-effect", "read") == "read":
                continue
            refs = [parameter.get("$ref", "") for parameter in operation["parameters"]]
            self.assertEqual(refs, ["#/components/parameters/IdempotencyKey"], path)

    def test_the_request_body_is_the_one_the_view_validates(self):
        spec = next(
            spec
            for spec in describe_capabilities()["capabilities"]
            if spec["name"] == "project.update"
        )
        self.assertEqual(
            self.document["components"]["schemas"]["ProjectUpdateRequest"],
            views._request_schema(spec),
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
        self.assertEqual(projects["tags"], ["hq.projects", "api.v2"])
        self.assertEqual(tags["hq.projects"]["parent"], "nav.build")

    def test_local_schema_definitions_are_hoisted(self):
        components = openapi._Components()
        hoisted = components.hoist(
            {
                "type": "object",
                "$defs": {"record": {"type": "object"}},
                "properties": {"r": {"$ref": "#/$defs/record"}},
            },
            "Owner",
        )
        self.assertNotIn("$defs", hoisted)
        self.assertEqual(hoisted["properties"]["r"]["$ref"], "#/components/schemas/OwnerRecord")
        self.assertIn("OwnerRecord", components.schemas)

    def test_a_parameterised_route_without_an_expansion_fails_the_build(self):
        with patch.dict(openapi.EXPANSIONS, clear=True):
            with self.assertRaises(openapi.OpenAPIError):
                openapi.document()

    def test_recorded_examples_conform_to_their_operations(self):
        contract = Contract(self.document)
        operations = {
            operation["operationId"]: (path, method)
            for path, method, operation in _operations(self.document)
        }
        examples = json.loads(openapi.EXAMPLES_PATH.read_text(encoding="utf-8"))
        self.assertTrue(examples)
        for operation_id, entry in examples.items():
            path, method = operations[operation_id]
            for status, body in entry.get("responses", {}).items():
                with self.subTest(operation=operation_id, status=status):
                    contract.validate(contract.response_pointer(path, method, int(status)), body)
            if "request" in entry:
                contract.validate(
                    ["paths", path, method, "requestBody", "content", "application/json", "schema"],
                    entry["request"],
                )


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
            response = self.client.get(
                reverse("hq_api:openapi"), HTTP_AUTHORIZATION=f"Bearer {_token()}"
            )
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

    def test_resource_collections_accept_projection_metadata(self):
        self._answer(
            "/api/v2/resources/projects/", 200,
            {"ok": True, "data": {"items": [], "count": 0, "filters": {"query": "example"}}},
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
