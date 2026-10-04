"""Bounded schema generation through Django's real in-process WSGI stack."""

from unittest.mock import Mock, patch

import schemathesis
from django.core import signals
from django.core.wsgi import get_wsgi_application
from django.db import close_old_connections, transaction
from django.test import TestCase, override_settings
from hypothesis import HealthCheck, given, settings

from application.capabilities import CapabilitySpec
from application.integrations import (
    compile_integration_graph,
    override_integration_graph,
)
from schemathesis.checks import not_a_server_error
from schemathesis.generation import GenerationMode
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from werkzeug.test import Client

from hq_sdk.capabilities import StrictCommand


@override_settings(
    ALLOWED_HOSTS=["localhost", "testserver"],
    SECURE_SSL_REDIRECT=False,
    OIDC_ISSUER="https://sso.example.com",
    SEVERINO_API_RESOURCE="https://hq.example.com/api",
)
class APIPropertyTests(TestCase):
    """Each generated operation reaches middleware and the authenticated adapter.

    Read-only authority keeps generated host commands away from external effects.
    Each example rolls back its observations, denials and any local mutations.
    """

    def setUp(self):
        # Any unexpected outbound connection fails rather than reaching a provider.
        self.enterContext(
            patch(
                "socket.socket.connect",
                side_effect=AssertionError(
                    "Outbound network access in API property test"
                ),
            )
        )
        self.enterContext(
            patch(
                "application.hq_self.host_addresses",
                return_value=frozenset({"192.0.2.1"}),
            )
        )
        # Match Django ClientHandler: WSGI request signals must preserve the
        # test runner's transaction rather than close its SQLite connection.
        for signal in (signals.request_started, signals.request_finished):
            signal.disconnect(close_old_connections)
            self.addCleanup(signal.connect, close_old_connections)

    def test_every_live_operation_conforms(self):
        claims = {
            "sub": "example-properties",
            "client_id": "example-properties",
            "scope": "read",
        }
        headers = {"Authorization": "Bearer example-properties"}
        checks = [
            not_a_server_error,
            status_code_conformance,
            content_type_conformance,
            response_schema_conformance,
        ]
        with (
            patch("hq_api.views.verify", return_value=claims),
            patch("hq_api.views.agents_paused", return_value=False),
        ):
            schema = schemathesis.openapi.from_wsgi(
                "/api/v2/openapi.json",
                get_wsgi_application(),
                headers=headers,
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            expected = {
                value["operationId"]
                for path in schema.raw_schema["paths"].values()
                for method, value in path.items()
                if method
                in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
            }
            # Schemathesis excludes the schema-serving operation itself.
            live = Client(schema.app).get(
                "/api/v2/openapi.json",
                headers=headers,
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            self.assertEqual(live.status_code, 200)
            self.assertEqual(live.json, schema.raw_schema)
            covered = {
                schema.raw_schema["paths"]["/api/v2/openapi.json"]["get"]["operationId"]
            }
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
                            response = case.call(
                                headers=headers,
                                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                            )
                            self.assertNotIn(response.status_code, (401, 503))
                            case.validate_response(response, checks=checks)
                            transaction.set_rollback(True)

                    exercise()
                    covered.add(operation.definition.raw["operationId"])
            self.assertEqual(covered, expected)

    def test_generated_authorized_write_and_retry(self):
        class ExampleCommand(StrictCommand):
            value: int = 0

        handler = Mock(return_value={"ok": True, "value": "example"})
        spec = CapabilitySpec(
            "example.properties",
            "Exercise a generated write.",
            "remote_write",
            "example.write",
            ExampleCommand,
            handler,
        )
        graph = compile_integration_graph(
            capabilities=(spec,), resources=(), connections=()
        )
        claims = {"sub": "example-properties", "scope": "read example.write"}
        headers = {
            "Authorization": "Bearer example-properties",
            "Idempotency-Key": "example-property",
        }
        with (
            override_integration_graph(graph),
            patch("hq_api.views.verify", return_value=claims),
            patch("hq_api.views.agents_paused", return_value=False),
        ):
            schema = schemathesis.openapi.from_wsgi(
                "/api/v2/openapi.json",
                get_wsgi_application(),
                headers=headers,
                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
            )
            operation = schema["/api/v2/capabilities/example.properties/"]["POST"]
            for mode in (GenerationMode.POSITIVE, GenerationMode.NEGATIVE):

                @settings(
                    max_examples=3, derandomize=True, database=None, deadline=None
                )
                @given(case=operation.as_strategy(generation_mode=mode))
                def exercise(case):
                    handler.reset_mock()
                    with transaction.atomic():
                        first = case.call(
                            headers=headers,
                            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                        )
                        case.validate_response(
                            first,
                            checks=[
                                not_a_server_error,
                                status_code_conformance,
                                content_type_conformance,
                                response_schema_conformance,
                            ],
                        )
                        if mode == GenerationMode.POSITIVE:
                            self.assertEqual(first.status_code, 200)
                            second = case.call(
                                headers=headers,
                                environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                            )
                            self.assertEqual(second.content, first.content)
                            self.assertEqual(
                                second.headers["idempotency-replayed"], ["true"]
                            )
                            handler.assert_called_once()
                        elif first.status_code >= 400:
                            handler.assert_not_called()
                        transaction.set_rollback(True)

                exercise()
