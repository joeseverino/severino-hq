"""A call sending a file as base64 is held to that file's own declared limit,
never to one body limit for every call."""

import base64
import json
from dataclasses import dataclass
from typing import Annotated

from django.test import TestCase, override_settings

from hq.domains.receipts.validation import MAX_RECEIPT_BYTES
from hq.platform.application.capabilities import execute_capability
from hq.platform.application.command_fields import Upload
from hq.platform.application.integration_specs import CapabilitySpec
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import compile_integration_graph
from hq.platform.application.receipts import ReceiptFile
from hq.platform.application.security import Principal

from .tokens import ISSUER, RESOURCE, _serving, _token

NAME = "example.file.upload"
MB = 1000 * 1000
# A host grant a token holds, since a grant no command in HQ names is dropped.
GRANT = "write_projects"


@dataclass(frozen=True, slots=True)
class ExampleKeep:
    file: Annotated[ReceiptFile, Upload(max_bytes=MAX_RECEIPT_BYTES)]


def _kept(command: ExampleKeep, *, principal) -> dict:
    return {"ok": True}


def _file(size: int) -> dict[str, str]:
    content = base64.b64encode(b"\0" * size).decode("ascii")
    return {"name": "scan.pdf", "content_type": "application/pdf", "content": content}


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_SITE_HOST="hq.example.com",
)
class FileCallTests(TestCase):
    def setUp(self):
        spec = CapabilitySpec(NAME, "Keep an example file.", "remote_write", GRANT, ExampleKeep, _kept)
        graph = compile_integration_graph(capabilities=(spec,), resources=(), connections=())
        self.enterContext(override_integration_graph(graph))
        self.enterContext(_serving())

    def _post(self, payload: dict, key: str):
        return self.client.post(
            f"/api/v2/capabilities/{NAME}/",
            data=json.dumps({"payload": payload}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {_token(scope=GRANT)}",
            HTTP_IDEMPOTENCY_KEY=key,
        )

    def test_a_file_within_its_limit_is_taken_whatever_its_size(self):
        response = self._post({"file": _file(12 * MB)}, "within")

        self.assertEqual(response.status_code, 200, response.content[:300])

    def test_a_file_over_its_limit_is_refused_in_the_fields_words(self):
        payload = {"file": _file(MAX_RECEIPT_BYTES + 1)}
        response = self._post(payload, "over")

        principal = Principal("example-automation", "api", frozenset({GRANT}))
        said = execute_capability(NAME, {**payload, "idempotency_key": "in-process"}, principal=principal)
        self.assertEqual(response.status_code, 400, response.content[:300])
        self.assertEqual(response.json()["error"]["message"], said["error"]["message"])

    def test_a_body_past_every_file_it_may_carry_is_refused_unread(self):
        response = self._post({"file": _file(MAX_RECEIPT_BYTES * 2)}, "past")

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["error"]["code"], "request_too_large")
