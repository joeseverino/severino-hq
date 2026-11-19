"""The API reads a list's filters as the MCP server does: a value outside a
filter's choices or past its limit is refused naming the field, and the
resource catalog narrows to one kind by name."""

from django.test import TestCase, override_settings

from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.application.projection import MAX_PAGE_SIZE
from hq.platform.core.testing import declare
from hq.platform.mcp.tests.test_list_filters import OUTSIDE, calls

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

KINDS = "/api/v2/resources/infrastructure/?kinds="


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class TheApiAgreesTests(TestCase):
    # Every response below is also held to the OpenAPI document.
    client_class = ContractClient

    def test_the_api_refuses_the_same_value_naming_the_field(self):
        with _serving():
            for path, field in (
                (f"/api/v2/resources/projects/?status={OUTSIDE}", "status"),
                (f"/api/v2/resources/infrastructure/?kind={OUTSIDE}", "kind"),
                (f"/api/v2/resources/projects/?limit={MAX_PAGE_SIZE + 1}", "limit"),
            ):
                with self.subTest(path=path):
                    response = self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}")
                    error = response.json()["error"]
                    self.assertIn(field, [detail["loc"][0] for detail in error["details"]["errors"]])

    def test_the_api_narrows_by_name_as_the_tool_does(self):
        with _serving():
            token = f"Bearer {_token(scope='read')}"
            narrowed = self.client.get("/api/v2/resources/?name=projects", HTTP_AUTHORIZATION=token)
            unknown = self.client.get(f"/api/v2/resources/?name={OUTSIDE}", HTTP_AUTHORIZATION=token)
        (answer,) = calls(("describe_resources", {"name": "projects"}))

        self.assertEqual(narrowed.status_code, 200)
        (entry,) = narrowed.json()["data"]["resources"]
        (tool_entry,) = answer.structured_content["resources"]
        self.assertEqual(entry["operations"], tool_entry["operations"])
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.json()["error"]["code"], "unknown_resource")

    def test_a_name_no_resource_has_is_one_refusal_on_both_doors(self):
        names = (OUTSIDE, "project", "cloudflare.dns_record", "adguard.client")
        with _serving():
            token = f"Bearer {_token(scope='read')}"
            api = [
                self.client.get(f"/api/v2/resources/?name={name}", HTTP_AUTHORIZATION=token).json()["error"]
                for name in names
            ]
        mcp = calls(*(("describe_resources", {"name": name}) for name in names))

        for name, through_api, through_mcp in zip(names, api, mcp, strict=True):
            with self.subTest(name=name):
                self.assertEqual(through_api["code"], "unknown_resource")
                self.assertEqual(through_mcp.structured_content["error"], through_api)

    def test_kinds_takes_every_value_its_schema_allows_and_refuses_the_rest(self):
        import re

        from hq.platform.application.infrastructure_specs import InfrastructureResourceQuery

        pattern = InfrastructureResourceQuery.model_json_schema()["properties"]["kinds"]["anyOf"][0]["pattern"]
        listed = sorted(PROVIDERS)
        with _serving():
            token = f"Bearer {_token(scope='read')}"
            for kinds in (listed[0], ",".join(listed), f"{listed[0]},{listed[0]}"):
                with self.subTest(kinds=kinds):
                    self.assertIsNotNone(re.fullmatch(pattern, kinds))
                    response = self.client.get(f"{KINDS}{kinds}", HTTP_AUTHORIZATION=token)
                    self.assertEqual(response.status_code, 200, response.content)
            for kinds in (OUTSIDE, f"{listed[0]},", f"{listed[0]},{OUTSIDE}"):
                with self.subTest(kinds=kinds):
                    self.assertIsNone(re.fullmatch(pattern, kinds))
                    response = self.client.get(f"{KINDS}{kinds}", HTTP_AUTHORIZATION=token)
                    self.assertEqual(response.status_code, 400)

    def test_kind_and_kinds_sent_together_each_narrow_the_list(self):

        declare("example-machine", "machine", {"name": "example"})
        declare("example-network", "network")
        with _serving():
            token = f"Bearer {_token(scope='read')}"
            for kind, kept in (("machine", ["example-machine"]), ("caddy.route", [])):
                with self.subTest(kind=kind):
                    response = self.client.get(f"{KINDS}machine,network&kind={kind}", HTTP_AUTHORIZATION=token)
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual([item["key"] for item in response.json()["data"]["items"]], kept)
