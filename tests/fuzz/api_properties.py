"""Bounded schema generation through Django's real in-process WSGI stack."""

from dataclasses import dataclass
from unittest.mock import Mock, patch

import schemathesis
from django.core.wsgi import get_wsgi_application
from django.db import transaction
from hypothesis import HealthCheck, given, settings
from schemathesis.checks import not_a_server_error
from schemathesis.generation import GenerationMode
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)
from werkzeug.test import Client

from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import (
    compile_integration_graph,
)
from hq.platform.application.security import host_capabilities

from .in_process import DOCUMENT, LOOPBACK, UNCOMPRESSED, InProcessAPITestCase, as_agent


class APIPropertyTests(InProcessAPITestCase):
    """Each generated operation reaches middleware and the authenticated adapter.

    Read-only authority keeps generated host commands away from external effects.
    Each example rolls back its observations, denials and any local mutations.
    """

    def test_every_live_operation_conforms(self):
        claims = {
            "sub": "example-properties",
            "client_id": "example-properties",
            "scope": "read",
        }
        headers = {"Authorization": "Bearer example-properties", **UNCOMPRESSED}
        checks = [
            not_a_server_error,
            status_code_conformance,
            content_type_conformance,
            response_headers_conformance,
            response_schema_conformance,
        ]
        with as_agent(claims):
            schema = schemathesis.openapi.from_wsgi(
                DOCUMENT, get_wsgi_application(), headers=headers, environ_overrides=LOOPBACK
            )
            expected = {
                value["operationId"]
                for path in schema.raw_schema["paths"].values()
                for method, value in path.items()
                if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
            }
            # Schemathesis excludes the schema-serving operation itself.
            live = Client(schema.app).get(DOCUMENT, headers=headers, environ_overrides=LOOPBACK)
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live.json, schema.raw_schema)
            covered = {schema.raw_schema["paths"][DOCUMENT]["get"]["operationId"]}
            for result in schema.get_all_operations():
                operation = result.ok()
                with self.subTest(operation=operation.label):

                    @settings(
                        max_examples=2,
                        derandomize=True,
                        database=None,
                        deadline=None,
                        suppress_health_check=[HealthCheck.too_slow],
                    )
                    @given(case=operation.as_strategy())
                    def exercise(case):
                        with transaction.atomic():
                            response = case.call(headers=headers, environ_overrides=LOOPBACK)
                            self.assertNotIn(response.status_code, (401, 503))
                            case.validate_response(response, checks=checks)
                            transaction.set_rollback(True)

                    exercise()
                    covered.add(operation.definition.raw["operationId"])
            self.assertEqual(covered, expected)

    def test_generated_authorized_write_and_retry(self):
        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            value: int = 0

        handler = Mock(return_value={"ok": True, "value": "example"})
        spec = CapabilitySpec(
            "example.properties.set",
            "Exercise a generated write.",
            "remote_write",
            "example.write",
            ExampleCommand,
            handler,
        )
        graph = compile_integration_graph(capabilities=(spec,), resources=(), connections=())
        claims = {"sub": "example-properties", "scope": "read example.write"}
        headers = {
            "Authorization": "Bearer example-properties",
            "Idempotency-Key": "example-property",
            **UNCOMPRESSED,
        }
        with (
            override_integration_graph(graph),
            as_agent(claims),
            # The injected capability belongs to no extension, so HQ is told it grants it.
            patch(
                "hq.platform.application.security.operator_capabilities",
                return_value=host_capabilities() | {"example.write"},
            ),
        ):
            schema = schemathesis.openapi.from_wsgi(
                DOCUMENT, get_wsgi_application(), headers=headers, environ_overrides=LOOPBACK
            )
            operation = schema["/api/v2/capabilities/example.properties.set/"]["POST"]
            for mode in (GenerationMode.POSITIVE, GenerationMode.NEGATIVE):

                @settings(max_examples=3, derandomize=True, database=None, deadline=None)
                @given(case=operation.as_strategy(generation_mode=mode))
                def exercise(case):
                    handler.reset_mock()
                    command = case.body.get("payload") if isinstance(case.body, dict) else None
                    if mode == GenerationMode.POSITIVE and isinstance(command, dict) and "idempotency_key" in command:  # noqa: B023 - exercise runs before the loop advances
                        # One key, however it arrives: a payload naming another than the header is refused.
                        command["idempotency_key"] = headers["Idempotency-Key"]
                    with transaction.atomic():
                        first = case.call(headers=headers, environ_overrides=LOOPBACK)
                        case.validate_response(
                            first,
                            checks=[
                                not_a_server_error,
                                status_code_conformance,
                                content_type_conformance,
                                response_headers_conformance,
                                response_schema_conformance,
                            ],
                        )
                        if mode == GenerationMode.POSITIVE:  # noqa: B023 - exercise runs before the loop advances
                            self.assertEqual(first.status_code, 200)
                            second = case.call(headers=headers, environ_overrides=LOOPBACK)
                            self.assertEqual(second.content, first.content)
                            self.assertEqual(second.headers["idempotency-replayed"], ["true"])
                            handler.assert_called_once()
                        elif first.status_code >= 400:
                            handler.assert_not_called()
                        transaction.set_rollback(True)

                exercise()
