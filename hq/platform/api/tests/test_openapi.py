"""The OpenAPI document is complete, and what the API really answers.

Complete means derived: every route, capability, resource operation and domain
appears without anyone listing it (application/tests/test_domain_declaration.py
proves a new domain's commands and resource arrive with no edit here). What the
API really answers is held by ``ContractClient`` on every TransportTests
request; the tests below cover the document's own route and the contract
client's refusals.
"""

import json
import re
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from jsonschema import ValidationError

from hq.platform.application.capabilities import capability_registry, describe_capabilities
from hq.platform.application.domains import all_domains
from hq.platform.application.idempotency import InvalidIdempotencyKey, validate_key
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD, IDEMPOTENCY_HEADER
from hq.platform.application.json_schemas import fields as whole_fields
from hq.platform.application.resources import resource_registry
from hq.platform.application.security import Capability, cli_principal, could_hold
from hq.platform.core.models import AuditLog
from hq.platform.core.test_runner import no_default_role

from .. import openapi, views
from ..testing import Contract, ContractClient
from .tokens import ISSUER, OTHER_KEY, RESOURCE, _serving, _token

# A list that matched nothing, with every field the collection contract requires.
EMPTY_PAGE = {"items": [], "count": 0, "total": 0, "truncated": False, "next": None}


def _last(reference: str) -> str:
    return reference.rsplit("/", 1)[-1]


def _resolved(document: dict, value):
    """``value`` with each reference into the document's schemas replaced by what it names."""

    if isinstance(value, list):
        return [_resolved(document, item) for item in value]
    if not isinstance(value, dict):
        return value
    if "$ref" in value and value["$ref"].startswith("#/components/schemas/"):
        named = document["components"]["schemas"][_last(value["$ref"])]
        return _resolved(document, {**named, **{key: item for key, item in value.items() if key != "$ref"}})
    return {key: _resolved(document, item) for key, item in value.items()}


def _named(document: dict, value: dict) -> dict:
    """What a bare reference into the document's components names, followed to the end."""

    while set(value) == {"$ref"}:
        section, name = value["$ref"].removeprefix("#/components/").split("/")
        value = document["components"][section][name]
    return value


def _unsaid(schema) -> dict:
    """A query field's schema without its words, which its parameter carries."""

    return {key: value for key, value in schema.items() if key != "description"}


def _operations(value: dict) -> list[tuple[str, str, dict]]:
    return [(path, method, operation) for path, item in value["paths"].items() for method, operation in item.items()]


class DocumentTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.document = openapi.document()

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

    def test_every_capability_a_token_could_hold_appears(self):
        found = {
            operation["x-hq-capability"]
            for _, _, operation in _operations(self.document)
            if "x-hq-capability" in operation
        }
        holdable = {
            name for name, spec in capability_registry().items() if could_hold("api", spec.required_capabilities)
        }
        self.assertEqual(found, holdable)
        self.assertNotIn("certificate.upload", found)

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

        resources = {
            spec["name"]: describe_resources(name=spec["name"])["resources"][0]
            for spec in describe_resources()["resources"]
        }
        for _, _, operation in _operations(self.document):
            name = operation.get("x-hq-resource")
            if not name or "x-hq-identifier" in operation:
                continue
            query = resources[name]["operations"]["list"]["query_schema"]
            expected = openapi._Components().hoist({**query, "properties": whole_fields(query)})
            written = [_named(self.document, parameter) for parameter in operation["parameters"]]
            parameters = {parameter["name"]: parameter for parameter in written if parameter["name"] != "fields"}
            self.assertEqual(set(parameters), set(expected.get("properties", {})))
            # The selection sits beside the query, never inside it.
            self.assertIn({"$ref": "#/components/parameters/Fields"}, operation["parameters"])
            for field, schema in expected.get("properties", {}).items():
                parameter = parameters[field]
                self.assertEqual(parameter["in"], "query")
                self.assertEqual(parameter["style"], "form")
                self.assertIs(parameter["explode"], True)
                self.assertEqual(parameter["required"], field in expected.get("required", []))
                # The field's words are the parameter's, said once beside its schema.
                self.assertEqual(
                    _resolved(self.document, parameter["schema"]), _resolved(self.document, _unsaid(schema))
                )
                self.assertEqual(parameter.get("description"), schema.get("description"))
                self.assertNotIn("content", parameter)

    def test_projection_parameters_are_declared_with_the_type_the_view_reads(self):
        from hq.platform.api import views

        operation = self.document["paths"]["/api/v2/findings/"]["get"]
        declared = {parameter["name"]: parameter["schema"] for parameter in operation["parameters"]}
        said = {parameter["name"]: parameter.get("description") for parameter in operation["parameters"]}

        fields = views.findings.__hq_query_fields__
        self.assertEqual(declared, {name: _unsaid(schema) for name, schema in fields.items()})
        self.assertEqual(said, {name: schema.get("description") for name, schema in fields.items()})
        self.assertEqual(declared["limit"]["type"], "integer")
        self.assertIn("400", operation["responses"])

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
            for spec in describe_capabilities(principal=cli_principal())["capabilities"]
            if spec["name"] == "project.update"
        )
        self.assertEqual(
            _resolved(self.document, self.document["components"]["schemas"]["ProjectUpdateRequest"]),
            _resolved(self.document, openapi._Components().hoist(views.request_schema(spec))),
        )

    def test_every_retry_key_is_the_one_the_header_takes(self):
        """A payload's key and the header are one schema, and it is the shape the key is validated by."""

        shared = {"$ref": "#/components/schemas/IdempotencyKey"}
        schemas = self.document["components"]["schemas"]
        header = self.document["components"]["parameters"]["IdempotencyKey"]
        self.assertEqual(header["name"], IDEMPOTENCY_HEADER)
        self.assertEqual(header["schema"], shared)
        written = 0
        for _, _, operation in _operations(self.document):
            if operation.get("x-hq-effect", "read") == "read":
                continue
            body = _named(self.document, operation["requestBody"]["content"]["application/json"]["schema"])
            key = _named(self.document, body["properties"]["payload"])["properties"][IDEMPOTENCY_FIELD]
            self.assertEqual(key, shared)
            written += 1
        self.assertTrue(written)
        pattern = re.compile(schemas["IdempotencyKey"]["pattern"])
        for value in ("a", "a" * 128, "example-key:1.2_3"):
            self.assertTrue(pattern.search(value) and len(value) <= schemas["IdempotencyKey"]["maxLength"])
            self.assertEqual(validate_key(value), value)
        for value in ("a" * 129, "-a", "a b", ""):
            self.assertFalse(pattern.search(value) and len(value) <= schemas["IdempotencyKey"]["maxLength"])
            with self.assertRaises(InvalidIdempotencyKey):
                validate_key(value)

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
        self.assertEqual(tags["hq.projects"]["parent"], "nav.projects")

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
        self.assertEqual(placed[len(leading) :], sorted(placed[len(leading) :], key=bar))
        seen: list[str] = []
        for tag in tags:
            if "parent" in tag:
                self.assertIn(tag["parent"], seen)
            seen.append(tag["name"])
        groups = [tag["name"] for tag in tags if tag["name"].startswith("nav.")]
        parents = [tag["parent"] for tag in tags if "parent" in tag]
        self.assertEqual(groups, list(dict.fromkeys(parents)))

    def test_what_belongs_to_no_domain_stands_under_the_api_where_the_bar_has_it(self):
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
        """Its declaring domain, else the domain of its web page or subject, else the API's own."""

        tags = openapi._Tags(resource_registry(), capability_registry())
        self.assertEqual(tags.of_resource("projects"), "hq.projects")
        self.assertEqual(tags.of_resource("services"), "hq.services")
        self.assertEqual(tags.of_resource("search"), openapi.API_TAG)
        self.assertEqual(tags.of_capability("project.create"), "hq.projects")
        self.assertEqual(tags.of_capability("pin.toggle"), openapi.API_TAG)
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
        components = openapi._Components()
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

    def test_two_definitions_meaning_different_things_under_one_name_fail_the_build(self):
        components = openapi._Components()
        components.hoist({"$defs": {"Record": {"type": "object"}}})

        with self.assertRaisesRegex(openapi.OpenAPIError, "Record"):
            components.hoist({"$defs": {"Record": {"type": "string"}}})

    def test_a_parameterised_route_without_an_expansion_fails_the_build(self):
        with patch.dict(openapi.EXPANSIONS, clear=True), self.assertRaises(openapi.OpenAPIError):
            openapi.document()

    def test_recorded_examples_conform_to_their_operations(self):
        contract = Contract(self.document)
        operations = {
            operation["operationId"]: (path, method) for path, method, operation in _operations(self.document)
        }
        examples = json.loads(openapi.EXAMPLES_PATH.read_text(encoding="utf-8"))
        self.assertTrue(examples)
        stale = "an example no longer fits its operation: `mise run fix:api-examples` records them again"
        for operation_id, entry in examples.items():
            with self.subTest(stale, operation=operation_id):
                path, method = operations[operation_id]
                for status, body in entry.get("responses", {}).items():
                    with self.subTest(stale, operation=operation_id, status=status):
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
