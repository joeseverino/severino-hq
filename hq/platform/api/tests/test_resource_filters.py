"""The API reads a list's filters as the MCP server does: a value outside a
filter's choices or past its limit is refused naming the field, and the
resource catalog narrows to one kind by name."""

from django.test import TestCase, override_settings

from hq.platform.application.projection import MAX_PAGE_SIZE
from hq.platform.mcp.tests.test_list_filters import OUTSIDE, calls

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


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
                    self.assertEqual(response.status_code, 400)
                    error = response.json()["error"]
                    self.assertEqual(error["code"], "invalid_input")
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
