"""Bounded schema generation through Django's real in-process WSGI stack.

Every operation the document declares is generated valid and invalid, called
with the authority it declares, and judged by Schemathesis's own checks: a
valid request is accepted, an invalid one refused with a declared refusal,
and a request without a good token refused.
"""

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

import schemathesis
from django.core import signals
from django.core.wsgi import get_wsgi_application
from django.db import close_old_connections, transaction
from django.test import TestCase, override_settings
from hypothesis import HealthCheck, given, settings
from hypothesis.errors import Unsatisfiable
from schemathesis.config import SchemathesisConfig
from schemathesis.generation import GenerationMode
from werkzeug.test import Client

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import compile_integration_graph
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.security import host_capabilities
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

MODES = (GenerationMode.POSITIVE, GenerationMode.NEGATIVE)
# The agent acts for a person, the estate's owner, so a capability that keeps
# something of a person's is reachable as well as one a machine may hold.
AGENT = "example-properties"
PERSON = "5b1e8c2a-7d3f-4a6e-9b0c-1f2e3d4c5b6a"
ENVIRON = {"REMOTE_ADDR": "127.0.0.1"}
# Bodies are read as the document describes them. This in-process client does
# not decode gzip; compression is the transport's, tested end to end on its own.
UNCOMPRESSED = {"Accept-Encoding": "identity"}
# What generates and what judges: the configuration Schemathesis reads.
SETTINGS = Path(__file__).with_name("schemathesis.toml")


def required(case) -> tuple[str, ...]:
    """The capabilities the document says ``case``'s call requires.

    A templated route (``/capabilities/{name}/``) declares none of its own:
    the call it makes is the operation the document lists at that path, when
    it lists one, and requires what that one declares.
    """

    raw = case.operation.schema.raw_schema["paths"]
    listed = raw.get(case.formatted_path, {}).get(case.method.lower(), {})
    return tuple(
        case.operation.definition.raw.get("x-hq-required-capabilities")
        or listed.get("x-hq-required-capabilities")
        or ()
    )


def bearer(scope) -> str:
    """A token an agent holds for the estate's owner, granting ``scope`` and the read every call needs."""

    return _token(AGENT, sub=PERSON, scope=" ".join(sorted({"read", *scope})))


class Grant:
    """Schemathesis's auth for each case: the authority its operation declares."""

    def get(self, case, context):
        return bearer(required(case))

    def set(self, case, data, context):
        case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}


def one_key(context, case):
    """A valid change carries one idempotency key, by the header or the payload.

    The document cannot say that the two, when both are sent, are the same
    key, nor that one of them is required.
    """

    if case.meta is None or not case.meta.generation.mode.is_positive:
        return case
    declared = {parameter.name for parameter in case.operation.headers}
    if "Idempotency-Key" in declared:
        payload = case.body.get("payload") if isinstance(case.body, dict) else None
        sent = payload.get("idempotency_key") if isinstance(payload, dict) else None
        case.headers = {
            **(case.headers or {}),
            "Idempotency-Key": sent or (case.headers or {}).get("Idempotency-Key") or str(uuid4()),
        }
    # A cursor and a stale-write token are values HQ issued, which no schema
    # draws: a drawn one names no page and no version.
    if case.query:
        case.query.pop("cursor", None)
    if isinstance(case.body, dict):
        case.body.pop("expected_updated_at", None)
    return case


@override_settings(
    ALLOWED_HOSTS=["localhost", "testserver"],
    SECURE_SSL_REDIRECT=False,
    OIDC_ISSUER=ISSUER,
    OIDC_RP_SIGN_ALGO="RS256",
    SEVERINO_API_RESOURCE=RESOURCE,
)
class APIPropertyTests(TestCase):
    """Each generated operation reaches middleware and the authenticated adapter.

    The showcase estate gives reads and targets records to find. Each example
    rolls back what it wrote, and every effect outside the database waits for
    a commit that never comes, so a generated write reaches nothing.
    """

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("showcase") as owner:
            bind_sign_in(owner, sign_in_subject(ISSUER, PERSON))

    def setUp(self):
        # No connection leaves the process: a provider is unreachable, as a
        # network that is down answers.
        self.enterContext(
            patch("socket.socket.connect", side_effect=ConnectionRefusedError("No network in API property test"))
        )
        self.enterContext(
            patch(
                "hq.platform.application.hq_self.host_addresses",
                return_value=frozenset({"192.0.2.1"}),
            )
        )
        self.enterContext(throwaway_media())
        self.enterContext(_serving())
        self.enterContext(patch("hq.platform.application.agent_access.paused_interfaces", return_value=frozenset()))
        # Match Django ClientHandler: WSGI request signals must preserve the
        # test runner's transaction rather than close its SQLite connection.
        for signal in (signals.request_started, signals.request_finished):
            signal.disconnect(close_old_connections)
            self.addCleanup(signal.connect, close_old_connections)

    def load(self):
        schema = schemathesis.openapi.from_wsgi(
            "/api/v2/openapi.json",
            get_wsgi_application(),
            headers={"Authorization": f"Bearer {bearer(())}"},
            environ_overrides=ENVIRON,
            config=SchemathesisConfig.from_path(SETTINGS),
        )
        schema.auth(refresh_interval=None)(Grant)
        schema.hook("map_case")(one_key)
        return schema

    def exercise(self, operation, mode, *, examples=None, after=None):
        examples = examples or operation.schema.config.generation_for(operation=operation).max_examples
        kwargs = {"headers": UNCOMPRESSED, "environ_overrides": ENVIRON}

        @settings(
            max_examples=examples,
            derandomize=True,
            database=None,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        )
        @given(case=operation.as_strategy(generation_mode=mode))
        def run(case):
            with transaction.atomic():
                response = case.call(**kwargs)
                case.validate_response(response, transport_kwargs=kwargs)
                if after is not None:
                    after(case, response)
                transaction.set_rollback(True)

        try:
            run()
        except Unsatisfiable:
            # An operation with nothing left to break once its bearer is the
            # configured one has no invalid request, as Schemathesis's own
            # runner finds before it schedules a mode.
            if not mode.is_negative:
                raise

    def test_every_live_operation_keeps_its_contract(self):
        schema = self.load()
        expected = {
            value["operationId"]
            for path in schema.raw_schema["paths"].values()
            for method, value in path.items()
            if method in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
        }
        # Schemathesis excludes the schema-serving operation itself.
        live = Client(schema.app).get(
            "/api/v2/openapi.json",
            headers={"Authorization": f"Bearer {bearer(())}", **UNCOMPRESSED},
            environ_overrides=ENVIRON,
        )
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json, schema.raw_schema)
        covered = {schema.raw_schema["paths"]["/api/v2/openapi.json"]["get"]["operationId"]}
        for result in schema.get_all_operations():
            operation = result.ok()
            for mode in MODES:
                with self.subTest(operation=operation.label, mode=mode.value):
                    self.exercise(operation, mode)
            covered.add(operation.definition.raw["operationId"])
        self.assertEqual(covered, expected)

    def test_generated_authorized_write_and_retry(self):
        """A retried change answers as it first did and runs once: durable idempotency, which no schema states."""

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
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
        graph = compile_integration_graph(capabilities=(spec,), resources=(), connections=())
        with (
            override_integration_graph(graph),
            # The injected capability belongs to no extension, so HQ is told it grants it.
            patch(
                "hq.platform.application.security.operator_capabilities",
                return_value=host_capabilities() | {"example.write"},
            ),
            patch(
                "hq.platform.application.roles.operator_capabilities",
                return_value=host_capabilities() | {"example.write"},
            ),
        ):
            operation = self.load()["/api/v2/capabilities/example.properties/"]["POST"]

            def retried(case, first):
                if not case.meta.generation.mode.is_positive:
                    if first.status_code >= 400:
                        handler.assert_not_called()
                    return
                second = case.call(headers=UNCOMPRESSED, environ_overrides=ENVIRON)
                self.assertEqual(second.content, first.content)
                self.assertEqual(second.headers["idempotency-replayed"], ["true"])
                handler.assert_called_once()

            for mode in MODES:
                with self.subTest(mode=mode.value):
                    handler.reset_mock()

                    def reset(case, response, retried=retried):
                        retried(case, response)
                        handler.reset_mock()

                    self.exercise(operation, mode, examples=3, after=reset)
