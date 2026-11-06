"""Over the machine API every failure is a refusal in the one shape, at its code's one status,
and every denial it answers is in the audit log once."""

import json
from dataclasses import replace
from unittest import mock

from django.test import TestCase, override_settings

from hq.platform.application.capabilities import capability_registry
from hq.platform.core.models import AuditLog
from hq.platform.core.tests import audit_rows

from ..testing import ContractClient
from .tokens import ISSUER, RESOURCE, _serving, _token

SECRET = "/srv/example/secret-path-7f3a"
ACTOR = "example-automation"


def _denials(since: int = 0):
    return audit_rows.since(since).filter(action=AuditLog.Action.DENIED)


def _failing(name: str, raised: Exception):
    """The registry with ``name``'s handler replaced by one that raises ``raised``."""

    registry = capability_registry()
    registry[name] = replace(registry[name], handler=mock.Mock(side_effect=raised))
    return mock.patch("hq.platform.application.capabilities.capability_registry", return_value=registry)


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    OIDC_RP_SIGN_ALGO="RS256",
)
class RefusalContractTests(TestCase):
    client_class = ContractClient

    def run_capability(self, name, scope, payload=None):
        with _serving():
            return self.client.post(
                f"/api/v2/capabilities/{name}/",
                data=json.dumps({"payload": payload or {}}),
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope=scope)}",
                HTTP_IDEMPOTENCY_KEY="example-key",
            )

    def read(self, path, scope):
        with _serving():
            return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {_token(scope=scope)}")

    def test_a_handler_that_raises_answers_operation_failed_and_the_log_keeps_its_traceback(self):
        body = {"name": "Example", "slug": "example", "status": "active"}
        with (
            _failing("project.create", RuntimeError(SECRET)),
            self.assertLogs("severino.capabilities", "ERROR") as logged,
        ):
            response = self.run_capability("project.create", "write_projects", body)

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response["Content-Type"], "application/json")
        answer = response.json()
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["code"], "operation_failed")
        self.assertNotIn(SECRET, response.content.decode())
        self.assertIsNotNone(logged.records[0].exc_info)

    def test_a_read_that_raises_answers_operation_failed_never_an_error_page(self):
        with (
            mock.patch("hq.platform.api.views.list_application_resource", side_effect=RuntimeError(SECRET)),
            self.assertLogs("severino.capabilities", "ERROR"),
        ):
            response = self.read("/api/v2/resources/projects/", "read")

        self.assertEqual(response.status_code, 500, response.content)
        self.assertEqual(response.json()["error"]["code"], "operation_failed")
        self.assertNotIn(SECRET, response.content.decode())

    def test_a_denied_write_leaves_one_denied_row_naming_the_caller(self):
        response = self.run_capability("project.create", "example.write")

        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(response.json()["error"]["code"], "insufficient_scope")
        row = _denials().get()
        self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("api", ACTOR))
        self.assertEqual(row.metadata["capability"], "project.create")

    def test_a_denied_read_leaves_one_denied_row_naming_the_caller(self):
        for path, scope in (("/api/v2/resources/audit/", "read"), ("/api/v2/topology/", "write_projects")):
            with self.subTest(path):
                before = audit_rows.mark()

                response = self.read(path, scope)

                self.assertEqual(response.status_code, 403, response.content)
                self.assertEqual(response.json()["error"]["code"], "insufficient_scope")
                row = _denials(before).get()
                self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("api", ACTOR))

    def test_an_unknown_resource_is_unknown_resource_at_404(self):
        response = self.read("/api/v2/resources/example.missing/", "read")

        self.assertEqual(response.status_code, 404, response.content)
        self.assertEqual(response.json()["error"]["code"], "unknown_resource")
