"""Bounded schema generation through Django's real in-process WSGI stack.

Every operation the document declares is generated valid and invalid, called
with the authority it declares, and judged by Schemathesis's own checks: a
valid request is accepted, an invalid one refused with a declared refusal,
and a request without a good token refused.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from unittest.mock import Mock, patch

import schemathesis
from django.core.wsgi import get_wsgi_application
from django.db import transaction
from django.test import override_settings
from hypothesis import HealthCheck, settings, strategies as st
from schemathesis.config import SchemathesisConfig
from schemathesis.generation import GenerationMode
from schemathesis.generation.hypothesis.builder import HypothesisTestConfig, HypothesisTestMode, create_test
from schemathesis.generation.meta import TestPhase
from schemathesis.hook_specs import flatmap_case, map_case
from werkzeug.test import Client

from hq.domains.control_plane.models import ProviderConnection
from hq.domains.control_plane.observations import OBSERVATIONS
from hq.domains.control_plane.providers import PROVIDERS
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.application.capabilities import CapabilitySpec
from hq.platform.application.command_fields import Describe
from hq.platform.application.domains import records_of
from hq.platform.application.host_sections import load
from hq.platform.application.input_errors import takes
from hq.platform.application.integration_specs import STALE_WRITE_KEYWORD
from hq.platform.application.integration_testing import override_integration_graph
from hq.platform.application.integrations import compile_integration_graph
from hq.platform.application.linked_accounts import bind_sign_in, sign_in_subject
from hq.platform.application.reading_records import record_filter
from hq.platform.application.security import host_capabilities
from hq.platform.application.stored_columns import ROW_RULES
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media
from hq.platform.core.tests.browser.shards import runs_here

from .in_process import DOCUMENT, LOOPBACK, UNCOMPRESSED, InProcessAPITestCase

# Schemathesis's own phases, as its runner and pytest plugin build them: the
# document's examples, the coverage cases its checks of headers, methods and
# media types judge, and the fuzzed cases of both modes.
PHASES = (HypothesisTestMode.EXAMPLES, HypothesisTestMode.COVERAGE, HypothesisTestMode.FUZZING)
FUZZING = HypothesisTestMode.FUZZING.value
# The agent acts for a person, the estate's owner, so a capability that keeps
# something of a person's is reachable as well as one a machine may hold.
AGENT = "example-properties"
PERSON = "5b1e8c2a-7d3f-4a6e-9b0c-1f2e3d4c5b6a"
# The document's one security scheme, whose requirement lists what a call needs.
BEARER = "bearer"
# What generates and what judges: the configuration Schemathesis reads.
SETTINGS = Path(__file__).with_name("schemathesis.toml")
# How many shares the live operations are exercised in, side by side, each by a worker of its own.
SHARES = 4


def _needs(operation: dict) -> tuple[str, ...]:
    return tuple(scope for requirement in operation.get("security", ()) for scope in requirement.get(BEARER, ()))


def listed(case):
    """The operation the document lists for ``case``'s concrete path, if any:
    the path itself, else the template with the fewest parameters that matches
    it, as a router prefers ``/resources/calendar/{identifier}/`` to
    ``/resources/{name}/{identifier}/``."""

    schema, method = case.operation.schema, case.method.lower()
    matching = [
        path
        for path, item in schema.raw_schema["paths"].items()
        if method in item and re.fullmatch(re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(path)), case.formatted_path)
    ]
    if not matching:
        return None
    return schema[min(matching, key=lambda path: path.count("{"))][case.method.upper()]


def required(case) -> tuple[str, ...]:
    """The capabilities the document's security requirement says ``case``'s call needs.

    A templated route (``/capabilities/{name}/``) requires none of its own:
    the call it makes is the operation the document lists at that path, when
    it lists one, and needs what that one requires.
    """

    named = listed(case)
    return _needs(case.operation.definition.raw) or (_needs(named.definition.raw) if named is not None else ())


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


def as_issued(context, case):
    """A valid call with each field held to a rule between its row's columns
    at its default, and each value only HQ issues left out or HQ's own.

    The document cannot compare two fields: it says such a rule in words, and
    the default is what every rule allows. Schemathesis's way for a field that
    depends on another is a hook ("Use `flatmap_<part>` when the strategy for
    one field depends on the value of another", ``map_`` where the change is
    deterministic).
    """

    if case.meta is None or not case.meta.generation.mode.is_positive:
        return case
    payload = case.body.get("payload") if isinstance(case.body, dict) else None
    if isinstance(payload, dict):
        for name in ruled(case.operation):
            payload.pop(name, None)
    # A stale-write token is a value HQ issued, which no schema draws: a drawn
    # one names no version.
    if isinstance(case.body, dict):
        case.body.pop(STALE_WRITE_KEYWORD, None)
    # So is a connection a declaration names: a sweep reported it, and a drawn
    # one names none, so the change is refused for want of a connection.
    spec = payload.get("spec") if isinstance(payload, dict) else None
    provider = PROVIDERS.get(payload.get("kind")) if isinstance(payload, dict) else None
    if isinstance(spec, dict) and "connection_ref" in spec and provider is not None:
        reported = ProviderConnection.objects.filter(provider__in=provider.connection_providers)
        spec["connection_ref"] = reported.values_list("connection_ref", flat=True).first() or ""
    # So is a record an import lays over: a drawn slug names none, so a record
    # sent without what creating one needs is refused for want of a stored one.
    if case.operation.path.endswith("/hq.import/") and isinstance(payload, dict):
        for kind in ("projects", "assets"):
            sent = [item for item in payload.get(kind) or () if isinstance(item, dict)]
            stored = load(records_of(kind).model).objects.values_list("slug", flat=True)[: len(sent)]
            for item, slug in zip(sent, stored, strict=False):
                item["slug"] = slug
    # So is a filter's member (`where[name]`, as sent), which only its kind's
    # records take, as they type it: a query parameter's schema cannot depend
    # on another's value (OAI/OpenAPI-Specification#256), so the document
    # closes `where` to the fields of every kind, and its words say which are
    # a kind's.
    if isinstance(case.query, dict) and case.operation.path.endswith("/reading.records/"):
        kind = case.query.get("kind")
        model = record_filter(kind).model if kind in OBSERVATIONS else None
        case.query = {
            key: value
            for key, value in case.query.items()
            if not key.startswith("where[") or (model is not None and takes(model, key[6:-1], value))
        }
    return case


def names_another(case) -> bool:
    """Whether ``case`` calls a templated route at a path the document lists an operation of its own for."""

    named = listed(case)
    return named is not None and named.path != case.operation.path


def by_name(context, case):
    """A valid call by a templated path that names an operation the document
    lists (``/capabilities/{name}/`` naming ``project.create``) sends what
    that operation takes: OpenAPI cannot tie a body or a query to a path
    parameter, and Schemathesis's way for a part that depends on another is
    ``flatmap`` ("Use `flatmap_<part>` when the strategy for one field depends
    on the value of another")."""

    if case.meta is None or not case.meta.generation.mode.is_positive or not names_another(case):
        return st.just(case)

    def sent(drawn):
        case.body, case.query = drawn.body, drawn.query
        case.headers = {**(case.headers or {}), **(drawn.headers or {})}
        return case

    return listed(case).as_strategy(generation_mode=GenerationMode.POSITIVE).map(sent)


def drawn_by_name(case) -> bool:
    """Whether a valid case of a templated path is one ``by_name`` drew: the
    examples and coverage phases apply no ``flatmap``, so their valid call
    naming a listed operation sends the template's body, which that
    operation's own run judges instead."""

    return not (
        case.meta is not None
        and case.meta.phase.name != TestPhase.FUZZING
        and case.meta.generation.mode.is_positive
        and names_another(case)
    )


@override_settings(OIDC_ISSUER=ISSUER, OIDC_RP_SIGN_ALGO="RS256", SEVERINO_API_RESOURCE=RESOURCE)
class APIProperties(InProcessAPITestCase):
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
        schema.hooks.register_hook_with_name(as_issued, map_case.__name__)
        schema.hooks.register_hook_with_name(by_name, flatmap_case.__name__)
        return schema

    def exercise(self, operation, *, examples=None, after=None):
        config = operation.schema.config
        examples = examples or config.generation_for(operation=operation, phase=FUZZING).max_examples
        kwargs = {"headers": UNCOMPRESSED, "environ_overrides": LOOPBACK}

        @settings(
            max_examples=examples,
            derandomize=True,
            database=None,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
        )
        def run(case):
            if not drawn_by_name(case):
                return
            with transaction.atomic():
                response = case.call(**kwargs)
                case.validate_response(response, transport_kwargs=kwargs)
                if after is not None:
                    after(case, response)
                transaction.set_rollback(True)

        modes = [mode for mode in PHASES if operation.is_testable_in(mode)]
        create_test(operation=operation, test_func=run, config=HypothesisTestConfig(project=config, modes=modes))()


class APIPropertyTests(APIProperties):
    def test_generated_authorized_write_and_retry(self):
        """A retried change answers as it first did and runs once: durable idempotency, which no schema states."""

        @dataclass(frozen=True, slots=True)
        class ExampleCommand:
            value: Annotated[str, Describe("Anything to set.")] = ""

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

            def reset(case, response):
                retried(case, response)
                handler.reset_mock()

            self.exercise(operation, examples=6, after=reset)


class _LiveOperations(APIProperties):
    """A share of the operations the document declares, each held to its contract. The shares together
    hold every one, each in a test class of its own, which the runner's workers take side by side."""

    share = (1, 1)

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
            if runs_here(operation.label, self.share):
                with self.subTest(operation=operation.label):
                    self.exercise(operation)
            covered.add(operation.definition.raw["operationId"])
        self.assertEqual(covered, expected)


# Each share a class of its own, by the name the runner reports it under. The base, whose share is every
# operation, leaves the module, so the loader never runs it beside them.
for _share in range(1, SHARES + 1):
    globals()[f"LiveOperationShare{_share}Tests"] = type(
        f"LiveOperationShare{_share}Tests", (_LiveOperations,), {"share": (_share, SHARES)}
    )
del _LiveOperations, _share
