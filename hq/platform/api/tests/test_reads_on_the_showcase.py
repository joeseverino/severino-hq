"""Every read the document lists answers on the showcase estate, a whole one,
as the document says, and what each answers is its recorded example
(``fix:api-examples``), so a viewer never draws an empty record from a schema."""

import os

from django.test import TestCase, override_settings

from hq.platform.application.tests.host_only import host_only
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from ..openapi import document
from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token


class ShowcaseClient(ContractClient):
    showcase = True


@override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
class ShowcaseReadTests(TestCase):
    client_class = ShowcaseClient

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase"):
            pass

    def test_every_listed_read_answers_the_showcase_with_its_recorded_example(self):
        paths = document()["paths"]
        # A concrete path: a templated one stands for the concrete ones listed beside it.
        reads = sorted(path for path, item in paths.items() if "get" in item and "{" not in path)
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        # The client holds each answer to the document as it comes.
        with _serving(), host_only():
            answered = [path for path in reads if self.client.get(path, **headers).status_code == 200]

        self.assertIn("/api/v2/resources/hq/", answered)
        # And a record of each listed resource by its identifier, so a record's
        # example is a set-up HQ's, never a test's bare rows.
        with _serving(), host_only():
            index = self.client.get("/api/v2/resources/", **headers).json()["data"]["resources"]
            for spec in index:
                found = (spec["operations"].get("get") or {}).get("identifier")
                listed = f"/api/v2/resources/{spec['name']}/"
                if not found or not spec["permitted"] or listed not in answered:
                    continue
                items = self.client.get(listed, **headers).json()["data"]["items"]
                if items:
                    with self.subTest(resource=spec["name"]):
                        detail = self.client.get(f"{listed}{items[0][found]}/", **headers)
                        self.assertEqual(detail.status_code, 200)
        if os.environ.get("HQ_API_RECORD_EXAMPLES") == "1":
            # This run writes the examples the assertion below reads.
            return
        # The document is its own example.
        unrecorded = [
            path
            for path in answered
            if path != "/api/v2/openapi.json"
            if "examples" not in paths[path]["get"]["responses"]["200"].get("content", {}).get("application/json", {})
        ]
        self.assertEqual(unrecorded, [], "a read the showcase answers with no recorded example")
