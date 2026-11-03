"""Each refusal the API answers carries a code its operation lists for that status,
and the contract client fails a test that receives one it does not."""

from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from jsonschema import ValidationError

from hq.platform.application.capabilities import capability_registry
from hq.platform.application.refusal_status import REFUSAL_STATUS
from hq.platform.application.security import AuthorizationError, PolicyDenied
from hq.platform.application.tests.host_only import host_only

from ..openapi import document
from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class RefusalCodeTests(TestCase):
    client_class = ContractClient

    def _capabilities(self, raised):
        with (
            _serving(),
            host_only(),
            mock.patch("hq.platform.api.views.describe_capabilities", side_effect=raised),
        ):
            return self.client.get("/api/v2/capabilities/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")

    def test_a_code_the_operation_lists_at_its_status_passes(self):
        self.assertEqual(self._capabilities(AuthorizationError("Refused.")).status_code, 403)

    def test_a_code_the_operation_does_not_list_at_its_status_fails_the_test(self):
        # A policy refusal is a 403 the capability catalog never answers.
        with self.assertRaises(ValidationError):
            self._capabilities(PolicyDenied("Refused."))


class ListedCodeTests(SimpleTestCase):
    def test_every_refusal_an_operation_lists_names_codes_of_its_status(self):
        value = document()
        components = value["components"]

        def last(reference):
            return reference.rsplit("/", 1)[-1]

        listed = 0
        for path, operations in value["paths"].items():
            for method, operation in operations.items():
                for status, answered in operation["responses"].items():
                    if int(status) < 400:
                        continue
                    response = components["responses"].get(last(answered.get("$ref", "")), answered)
                    schema = components["schemas"][last(response["content"]["application/json"]["schema"]["$ref"])]
                    code = schema["properties"]["error"]["properties"]["code"]
                    codes = [code["const"]] if "const" in code else code["enum"]
                    with self.subTest(path=path, method=method, status=status):
                        self.assertTrue(codes)
                        self.assertEqual({REFUSAL_STATUS[code] for code in codes}, {int(status)})
                    listed += 1
        self.assertGreater(listed, len(value["paths"]))


# A change's own codes, its retry key's and its hold's, written out so the
# tests run against code that derives none.
CHANGE_CODES = frozenset(
    {"idempotency_key_required", "invalid_idempotency_key", "idempotency_conflict", "refused_hold"}
)
# What a handler raises on its own, which no capability declares.
HANDLER_CODES = frozenset({"not_found", "conflict", "precondition", "rejected", "invalid_input"})


class CapabilityCodeListTests(SimpleTestCase):
    """Each capability's operation lists only what its declaration lets it answer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        value = document()
        cls.paths = value["paths"]
        cls.responses = value["components"]["responses"]
        cls.schemas = value["components"]["schemas"]

    def _listed(self, operation):
        codes = set()
        for status, answered in operation["responses"].items():
            if int(status) < 400:
                continue
            response = self.responses.get(answered.get("$ref", "").rsplit("/", 1)[-1], answered)
            schema = self.schemas[response["content"]["application/json"]["schema"]["$ref"].rsplit("/", 1)[-1]]
            code = schema["properties"]["error"]["properties"]["code"]
            codes |= {code["const"]} if "const" in code else set(code["enum"])
        return codes

    def test_only_a_call_by_name_lists_an_unknown_capability(self):
        self.assertIn("unknown_capability", self._listed(self.paths["/api/v2/capabilities/{name}/"]["post"]))
        operations = [op for ops in self.paths.values() for op in ops.values() if "x-hq-capability" in op]
        self.assertTrue(operations)
        for operation in operations:
            with self.subTest(capability=operation["x-hq-capability"]):
                self.assertNotIn("unknown_capability", self._listed(operation))

    def test_each_capability_lists_its_target_s_code_its_change_s_and_its_handler_s(self):
        registry = capability_registry()
        effects = set()
        for operations in self.paths.values():
            for operation in operations.values():
                if "x-hq-capability" not in operation:
                    continue
                spec = registry[operation["x-hq-capability"]]
                effects.add(spec.effect == "read")
                listed = self._listed(operation)
                with self.subTest(capability=spec.name):
                    self.assertEqual("target_required" in listed, bool(spec.target_kind))
                    self.assertEqual("target_not_allowed" in listed, not spec.target_kind)
                    if spec.effect == "read":
                        self.assertFalse(listed & CHANGE_CODES)
                    else:
                        self.assertLessEqual(CHANGE_CODES, listed)
                    self.assertLessEqual(HANDLER_CODES, listed)
        # Both kinds of capability were read.
        self.assertEqual(effects, {True, False})
