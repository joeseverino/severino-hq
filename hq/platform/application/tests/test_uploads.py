"""Every file a command takes is held to its own declared limit, on every door,
and the proxy in front of HQ lets through every upload HQ takes."""

import base64
import json
import math
from dataclasses import dataclass
from typing import Annotated
from unittest import mock

from django.test import TestCase, override_settings
from pydantic import Field

from hq.domains.control_plane.models import ManagedResource
from hq.domains.receipts.validation import MAX_RECEIPT_BYTES
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.core.testing import declare, managing_everything

from ..capabilities import execute_capability
from ..command_fields import NonEmpty, RequiredText, Upload
from ..integration_specs import CapabilitySpec
from ..integration_testing import override_integration_graph
from ..integrations import compile_integration_graph
from ..projection import projection_scope
from ..receipts import ReceiptFile
from ..security import Capability, Principal, mcp_principal

HQ = "hq.example.com"
EVERYTHING = Principal("test", "operator", frozenset(Capability))


@override_settings(SEVERINO_SITE_HOST=HQ, ALLOWED_HOSTS=[HQ, "testserver"])
class ProxyInFrontOfHQTests(TestCase):
    def test_a_proxy_refusing_an_upload_hq_takes_is_raised_and_its_press_lets_every_upload_through(self):
        from ..findings import estate_findings

        managing_everything()
        spec = {"domain_names": [HQ], "forward_scheme": "http", "forward_host": "192.0.2.10", "forward_port": 8000}
        declare("example-proxy", "npm.proxy_host", {**spec, "force_ssl": False, "client_max_body_size": 1})

        with projection_scope():
            (raised,) = estate_findings(principal=EVERYTHING, rule="proxy-refuses-uploads")
        (press,) = raised.presses
        answer = execute_capability(
            press.capability, {"idempotency_key": "once"}, principal=mcp_principal(), target="example-proxy"
        )
        self.assertTrue(answer["ok"], answer)

        limit = ManagedResource.objects.get(key="example-proxy").spec["client_max_body_size"]
        # A receipt fits, with room for the form around it, and so does one sent as base64 in a call.
        self.assertGreater(limit, math.ceil(MAX_RECEIPT_BYTES / 1024**2))
        self.assertGreaterEqual(limit * 1024**2, 4 * math.ceil(MAX_RECEIPT_BYTES / 3))
        with projection_scope():
            self.assertEqual(estate_findings(principal=EVERYTHING, rule="proxy-refuses-uploads"), ())

    def test_the_press_leaves_a_proxy_in_front_of_something_else_as_it_is(self):
        from ..resource_operations import ALLOW_UPLOADS

        managing_everything()
        spec = {"domain_names": ["app.example.com"], "forward_scheme": "http", "forward_host": "192.0.2.11"}
        declare("example-other", "npm.proxy_host", {**spec, "forward_port": 80, "client_max_body_size": 1})

        answer = execute_capability(
            ALLOW_UPLOADS, {"idempotency_key": "once"}, principal=mcp_principal(), target="example-other"
        )
        self.assertFalse(answer["ok"], answer)
        self.assertEqual(ManagedResource.objects.get(key="example-other").spec["client_max_body_size"], 1)


MIB = 1024 * 1024
DOCUMENT = Upload(max_bytes=MIB)
# A host grant a token holds, since a grant no command in HQ names is dropped.
GRANT = "write_projects"


@dataclass(frozen=True, slots=True)
class ExampleDocument:
    name: RequiredText
    content: str


@dataclass(frozen=True, slots=True)
class ExampleImport:
    """Shaped as an extension's import is: a list of documents, each a file."""

    documents: Annotated[list[Annotated[ExampleDocument, DOCUMENT]], NonEmpty(), Field(max_length=2)]


@dataclass(frozen=True, slots=True)
class ExampleKeep:
    file: Annotated[ReceiptFile, Upload(max_bytes=10)]


def _ran(command, *, principal) -> dict:
    return {"ok": True}


def _documents(*lengths: int) -> list[dict[str, str]]:
    return [{"name": f"export-{at}.csv", "content": "A" * length} for at, length in enumerate(lengths)]


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_SITE_HOST="hq.example.com",
)
class DeclaredFileTests(TestCase):
    def setUp(self):
        self.ran = mock.Mock(wraps=_ran)
        specs = (
            CapabilitySpec(
                "example.document.import", "Import documents.", "remote_write", GRANT, ExampleImport, self.ran
            ),
            CapabilitySpec("example.file.upload", "Keep a file.", "remote_write", GRANT, ExampleKeep, self.ran),
        )
        graph = compile_integration_graph(capabilities=specs, resources=(), connections=())
        self.enterContext(override_integration_graph(graph))
        self.enterContext(_serving())

    def _post(self, payload: dict, key: str):
        return self.client.post(
            "/api/v2/capabilities/example.document.import/",
            data=json.dumps({"payload": payload}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_token(scope=GRANT)}",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_a_call_carrying_every_file_its_list_may_hold_is_taken(self):
        response = self._post({"documents": _documents(DOCUMENT.max_encoded, DOCUMENT.max_encoded)}, "within")

        self.assertEqual(response.status_code, 200, response.content[:300])

    def test_a_body_past_every_file_its_list_may_hold_is_refused_unread(self):
        response = self._post({"documents": _documents(2 * DOCUMENT.max_encoded, DOCUMENT.max_encoded)}, "past")

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
        self.ran.assert_not_called()

    def test_a_file_past_its_limit_is_refused_as_sent(self):
        response = self._post({"documents": _documents(DOCUMENT.max_encoded + 1)}, "over")

        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertEqual(response.json()["error"]["details"][0]["loc"], ["documents", 0])
        self.ran.assert_not_called()

    def test_a_file_its_type_decodes_is_held_to_its_bytes(self):
        principal = Principal("example-automation", "api", frozenset({GRANT}))
        for size, ok in ((10, True), (11, False)):
            content = base64.b64encode(b"x" * size).decode()
            file = {"name": "scan.pdf", "content_type": "application/pdf", "content": content}
            with self.subTest(size=size):
                said = execute_capability(
                    "example.file.upload", {"file": file, "idempotency_key": str(size)}, principal=principal
                )
                self.assertEqual(said["ok"], ok, said)

    def test_a_file_longer_than_its_limit_encodes_to_is_never_decoded(self):
        principal = Principal("example-automation", "api", frozenset({GRANT}))
        file = {"name": "scan.pdf", "content_type": "application/pdf", "content": "A" * 20}
        with mock.patch.object(base64, "b64decode", wraps=base64.b64decode) as decode:
            said = execute_capability(
                "example.file.upload", {"file": file, "idempotency_key": "long"}, principal=principal
            )

        self.assertEqual(said["error"]["details"][0]["loc"], ["file"])
        decode.assert_not_called()
