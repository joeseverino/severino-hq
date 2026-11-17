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
from django.core.wsgi import get_wsgi_application
from django.db import transaction
from django.test import override_settings
from hypothesis import HealthCheck, given, settings, strategies as st
from hypothesis.errors import Unsatisfiable
from schemathesis.config import SchemathesisConfig
from schemathesis.generation import GenerationMode
from schemathesis.hook_specs import flatmap_case, map_case
from werkzeug.test import Client

from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.integration_specs import IDEMPOTENCY_FIELD, IDEMPOTENCY_HEADER, STALE_WRITE_KEYWORD
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import compile_integration_graph
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.security import host_capabilities
from hq.platform.application.stored_columns import ROW_RULES
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .in_process import DOCUMENT, LOOPBACK, UNCOMPRESSED, InProcessAPITestCase

MODES = (GenerationMode.POSITIVE, GenerationMode.NEGATIVE)
# The agent acts for a person, the estate's owner, so a capability that keeps
# something of a person's is reachable as well as one a machine may hold.
AGENT = "example-properties"
PERSON = "5b1e8c2a-7d3f-4a6e-9b0c-1f2e3d4c5b6a"
# The document's one security scheme, whose requirement lists what a call needs.
BEARER = "bearer"
# What generates and what judges: the configuration Schemathesis reads.
SETTINGS = Path(__file__).with_name("schemathesis.toml")


def _needs(operation: dict) -> tuple[str, ...]:
    return tuple(scope for requirement in operation.get("security", ()) for scope in requirement.get(BEARER, ()))


def required(case) -> tuple[str, ...]:
    """The capabilities the document's security requirement says ``case``'s call needs.

    A templated route (``/capabilities/{name}/``) requires none of its own:
    the call it makes is the operation the document lists at that path, when
    it lists one, and needs what that one requires.
    """

    listed = case.operation.schema.raw_schema["paths"].get(case.formatted_path, {}).get(case.method.lower(), {})
    return _needs(case.operation.definition.raw) or _needs(listed)


def bearer(scope) -> str:
    """A token an agent holds for the estate's owner, granting ``scope`` and the read every call needs."""

    return _token(AGENT, sub=PERSON, scope=" ".join(sorted({"read", *scope})))


class Grant:
    """Schemathesis's auth for each case: the authority its operation declares."""

    def get(self, case, context):
        return bearer(required(case))

    def set(self, case, data, context):
        case.headers = {**(case.headers or {}), "Authorization": f"Bearer {data}"}


def _resolved(root: dict, node: dict) -> dict:
    while "$ref" in node:
        node = {
            **root["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]],
            **{k: v for k, v in node.items() if k != "$ref"},
        }
    return node


def ruled(operation) -> frozenset[str]:
    """The payload fields ``operation`` says are held to rules between its row's columns."""

    root = operation.schema.raw_schema
    body = operation.definition.raw.get("requestBody", {}).get("content", {}).get("application/json", {})
    payload = _resolved(root, _resolved(root, body.get("schema", {})).get("properties", {}).get("payload", {}))
    return frozenset(name for name, field in payload.get("properties", {}).items() if ROW_RULES in field)


def one_key(context, case):
    """A valid change carries one idempotency key, by the header or the payload,
    and each field held to a rule between its row's columns at its default.

    The document cannot say that the two keys, when both are sent, are the
    same key, nor that one of them is required. Nor can it compare two fields:
    it says such a rule in words, and the default is what every rule allows.
    Schemathesis's way for a field that depends on another is a hook ("Use
    `flatmap_<part>` when the strategy for one field depends on the value of
    another", ``map_`` where the change is deterministic).
    """

    if case.meta is None or not case.meta.generation.mode.is_positive:
        return case
    payload = case.body.get("payload") if isinstance(case.body, dict) else None
    if isinstance(payload, dict):
        for name in ruled(case.operation):
            payload.pop(name, None)
    if IDEMPOTENCY_HEADER in {parameter.name for parameter in case.operation.headers}:
        sent = payload.get(IDEMPOTENCY_FIELD) if isinstance(payload, dict) else None
        case.headers = {
            **(case.headers or {}),
            IDEMPOTENCY_HEADER: sent or (case.headers or {}).get(IDEMPOTENCY_HEADER) or str(uuid4()),
        }
    # A cursor and a stale-write token are values HQ issued, which no schema
    # draws: a drawn one names no page and no version.
    if case.query:
        case.query.pop("cursor", None)
    if isinstance(case.body, dict):
        case.body.pop(STALE_WRITE_KEYWORD, None)
    return case


def by_name(context, case):
    """A valid call by a templated path that names an operation the document
    lists (``/capabilities/{name}/`` naming ``project.create``) sends what
    that operation takes: OpenAPI cannot tie a body or a query to a path
    parameter, and Schemathesis's way for a part that depends on another is
    ``flatmap`` ("Use `flatmap_<part>` when the strategy for one field depends
    on the value of another")."""

    method = case.method.upper()
    listed = case.operation.schema.raw_schema["paths"].get(case.formatted_path, {})
    if (
        case.meta is None
        or not case.meta.generation.mode.is_positive
        or case.formatted_path == case.operation.path
        or method.lower() not in listed
    ):
        return st.just(case)
    named = case.operation.schema[case.formatted_path][method]

    def sent(drawn):
        case.body, case.query = drawn.body, drawn.query
        case.headers = {**(case.headers or {}), **(drawn.headers or {})}
        return case

    return named.as_strategy(generation_mode=GenerationMode.POSITIVE).map(sent)


@override_settings(OIDC_ISSUER=ISSUER, OIDC_RP_SIGN_ALGO="RS256", SEVERINO_API_RESOURCE=RESOURCE)
class APIPropertyTests(InProcessAPITestCase):
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
        super().setUp()
        # No connection leaves the process: a provider is unreachable, as a
        # network that is down answers.
        self.enterContext(
            patch("socket.socket.connect", side_effect=ConnectionRefusedError("No network in API property test"))
        )
        self.enterContext(throwaway_media())
        self.enterContext(_serving())
        self.enterContext(patch("hq.platform.application.agent_access.paused_interfaces", return_value=frozenset()))

    def load(self):
        schema = schemathesis.openapi.from_wsgi(
            DOCUMENT,
            get_wsgi_application(),
            headers={"Authorization": f"Bearer {bearer(())}"},
            environ_overrides=LOOPBACK,
            config=SchemathesisConfig.from_path(SETTINGS),
        )
        schema.auth(refresh_interval=None)(Grant)
        # Named from its spec, which reading it loads: hooks.py's own import of the specs is lazy here.
        schema.hooks.register_hook_with_name(one_key, map_case.__name__)
        schema.hooks.register_hook_with_name(by_name, flatmap_case.__name__)
        return schema

    def exercise(self, operation, mode, *, examples=None, after=None):
        examples = examples or operation.schema.config.generation_for(operation=operation).max_examples
        kwargs = {"headers": UNCOMPRESSED, "environ_overrides": LOOPBACK}

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
            DOCUMENT, headers={"Authorization": f"Bearer {bearer(())}", **UNCOMPRESSED}, environ_overrides=LOOPBACK
        )
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json, schema.raw_schema)
        covered = {schema.raw_schema["paths"][DOCUMENT]["get"]["operationId"]}
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
            "example.properties.set",
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
            operation = self.load()["/api/v2/capabilities/example.properties.set/"]["POST"]

            def retried(case, first):
                if not case.meta.generation.mode.is_positive:
                    if first.status_code >= 400:
                        handler.assert_not_called()
                    return
                second = case.call(headers=UNCOMPRESSED, environ_overrides=LOOPBACK)
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
