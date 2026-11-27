"""The resource catalog refuses a name no resource has alike on the API and
MCP, and a list narrows by ``kind`` and ``kinds`` sent together."""

from django.test import TestCase, override_settings

from hq.platform.core.testing import declare
from hq.platform.mcp.tests.test_list_filters import OUTSIDE, calls

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

KINDS = "/api/v2/resources/infrastructure/?kinds="


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class TheApiAgreesTests(TestCase):
    # Every response below is also held to the OpenAPI document.
    client_class = ContractClient

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
