"""Schemathesis's stateful phase walks the API document's links over the example estate.

Each scenario starts at a list or a write and follows the links its answers
carry (``openapi._Builder.links``): a page to the next, an item to its read, a
record to the commands on it, a held change to its approval, a run's ask to
the run. The conformance checks hold every answer to the document; the three
below hold what a link promises.
"""

import uuid
from typing import Any

import schemathesis
from django.core.wsgi import get_wsgi_application
from django.db import transaction
from hypothesis import HealthCheck, settings
from schemathesis.checks import CheckContext, not_a_server_error
from schemathesis.config import SchemathesisConfig
from schemathesis.core.result import Ok
from schemathesis.generation.stateful import run_state_machine_as_test
from schemathesis.hook_specs import map_query
from schemathesis.specs.openapi.checks import (
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    status_code_conformance,
)

from hq.domains.projects.models import Project
from hq.platform.application.capabilities import capability_registry
from hq.platform.application.held_answer import HELD
from hq.platform.application.integration_specs import (
    IDEMPOTENCY_HEADER,
    PAGE_FIELD,
    STALE_WRITE_KEYWORD,
    takes_stale_write_token,
)
from hq.platform.application.projection import MAX_PAGE_SIZE
from hq.platform.application.run_reads import RUNS
from hq.platform.core import seeds
from hq.platform.core.example_estate.testing import throwaway_media

from .in_process import DOCUMENT, LOOPBACK, UNCOMPRESSED, InProcessAPITestCase, as_agent

# The records whose writes stay in HQ's database, which a scenario's rollback undoes whole.
LOCAL_RECORDS = frozenset({"projects", "assets", "content"})
# Their reads, and the reads a held change and a playbook's run lead to. Every
# other read is api_properties' to generate; spread over all of them, a walk
# this long would reach each list's next page too rarely to hold it.
WALKED = LOCAL_RECORDS | {HELD, RUNS}
# Only the links the document declares are walked, never ones Schemathesis infers.
CONFIG = SchemathesisConfig.from_dict(
    {"phases": {"stateful": {"inference": {"algorithms": []}, "generation": {"mode": "positive"}}}}
)
# About 5,600 steps, some 25 s on an 8 GB Mac under other worktrees' gates: each
# planted break of a link's promise is found within its first 2,000.
SCENARIOS = 300
STEPS = 25
# The query field a page's size is asked by.
PAGE_SIZE = "limit"


def _linked(ctx: CheckContext, case: Any) -> tuple[Any, Any, Any] | None:
    """The transition ``case`` followed, the answer it followed it from and that answer's request, or None for a
    scenario's first step."""

    recorder = ctx.internals.recorder
    node = recorder.cases.get(case.id) if recorder is not None else None
    if node is None or node.transition is None or node.parent_id is None:
        return None
    parent = recorder.find_response(case_id=node.parent_id)
    if parent is None or parent.status_code != 200:
        return None
    return node.transition, parent, recorder.cases[node.parent_id].value


def _value(extracted: Any) -> Any:
    return extracted.value.ok() if extracted is not None and isinstance(extracted.value, Ok) else None


def _beside(query: dict[str, Any]) -> dict[str, Any]:
    """A list's query without its page: what a next page is asked by besides its number."""

    return {name: value for name, value in query.items() if name != PAGE_FIELD}


def a_next_page_moves_on(ctx: CheckContext, response: Any, case: Any) -> None:
    """The page a list's next names holds none of the records of the page that named it."""

    linked = _linked(ctx, case)
    if linked is None or response.status_code != 200 or not isinstance(case.query, dict):
        return
    transition, parent, asked = linked
    page = _value(transition.parameters.get("query", {}).get(PAGE_FIELD))
    if page is None or case.query.get(PAGE_FIELD) != page:
        return
    # Schemathesis sends each linked value only now and then, so a page asked at
    # another size or order than the one that named it is not the page it named;
    # and a write since moves rows between pages, as a page number allows.
    steps = list(ctx.internals.recorder.cases)
    since = steps[steps.index(asked.id) + 1 : steps.index(case.id)]
    written = any(ctx.internals.recorder.cases[step].value.method.upper() != "GET" for step in since)
    if written or _beside(case.query) != _beside(asked.query or {}):
        return
    before = parent.json()["data"]["items"]
    repeated = [item for item in response.json()["data"]["items"] if item in before]
    assert not repeated, f"The page after a page repeats {len(repeated)} of its records."


def a_linked_record_reads(ctx: CheckContext, response: Any, case: Any) -> None:
    """A read named by an answer finds what it names, unless the scenario has since removed it."""

    linked = _linked(ctx, case)
    if linked is None or case.method.upper() != "GET" or response.status_code != 404:
        return
    transition, _, _ = linked
    named = _value(transition.parameters.get("path_parameters", {}).get("identifier"))
    if named is None or (case.path_parameters or {}).get("identifier") != named:
        return
    acted_on = [
        earlier.value.body.get("target")
        for earlier in ctx.internals.recorder.cases.values()
        if earlier.value.method.upper() == "POST" and isinstance(earlier.value.body, dict)
    ]
    assert str(named) in map(str, acted_on), f"{case.operation.label} does not find {named!r}, which an answer named."


def a_stale_write_is_refused(ctx: CheckContext, response: Any, case: Any) -> None:
    """A write that sends another `updated_at` than the record's, as read, changes nothing."""

    linked = _linked(ctx, case)
    if linked is None or response.status_code != 200 or not isinstance(case.body, dict):
        return
    transition, _, _ = linked
    read = _value(transition.request_body)
    if not isinstance(read, dict) or STALE_WRITE_KEYWORD not in read:
        return
    sent = case.body.get(STALE_WRITE_KEYWORD)
    if sent is None or case.body.get("target") != read.get("target") or sent == read[STALE_WRITE_KEYWORD]:
        return
    raise AssertionError(f"{case.operation.label} wrote with an `expected_updated_at` the record's is not, {sent!r}.")


CHECKS = [
    not_a_server_error,
    status_code_conformance,
    content_type_conformance,
    response_headers_conformance,
    response_schema_conformance,
    a_next_page_moves_on,
    a_linked_record_reads,
    a_stale_write_is_refused,
]


def _paging_only(context: Any, query: dict[str, Any] | None) -> dict[str, Any] | None:
    """A generated query cut to its page size: a random filter empties most lists, which
    then have no next page to walk to, and filters are api_properties' to generate."""

    return {name: value for name, value in query.items() if name == PAGE_SIZE} if query else query


def _writes() -> dict[str, tuple[str, ...]]:
    """The commands on a local record that check for a stale write, with what each needs."""

    return {
        spec.name: tuple(spec.required_capabilities)
        for spec in capability_registry()
        if spec.subject_resource in LOCAL_RECORDS and takes_stale_write_token(spec.handler)
    }


class APILinkTests(InProcessAPITestCase):
    """The local records' reads and writes, walked as their links lead, each scenario rolled back."""

    @classmethod
    def setUpTestData(cls):
        with throwaway_media(), seeds.built("standard"):
            pass
        # More than the largest page, so every limit a scenario draws leaves a page after the first.
        Project.objects.bulk_create(
            Project(name=f"Example {n:03d}", slug=f"example-{n:03d}") for n in range(MAX_PAGE_SIZE + 1)
        )

    def test_each_answer_leads_where_its_links_say(self):
        writes = _writes()
        scopes = sorted({"read", *(scope for needed in writes.values() for scope in needed)})
        claims = {"sub": "example-links", "client_id": "example-links", "scope": " ".join(scopes)}
        headers = {"Authorization": "Bearer example-links", **UNCOMPRESSED}
        with as_agent(claims):
            schema = schemathesis.openapi.from_wsgi(
                DOCUMENT, get_wsgi_application(), config=CONFIG, headers=headers, environ_overrides=LOOPBACK
            ).include(
                lambda ctx: (
                    ctx.operation.definition.raw.get("x-hq-resource") in WALKED
                    or ctx.operation.definition.raw.get("x-hq-capability") in writes
                )
            )

            # Schemathesis hands a hook only what it generates, never a value a link set
            # (``get_parameters_value`` in schemathesis/specs/openapi/_hypothesis.py).
            # Named from its spec, which reading it loads: hooks.py's own import of the specs is lazy here.
            schema.hooks.register_hook_with_name(_paging_only, map_query.__name__)

            # Schemathesis 4.33.4 builds one rule per link and starts a scenario at a
            # list or a write (specs/openapi/stateful/__init__.py, create_state_machine
            # and is_likely_root_transition), calls setup and teardown once a scenario
            # (generation/stateful/state_machine.py, APIStateMachine.setup), and sends
            # a link's value or, now and then, a generated one in its place
            # (specs/openapi/stateful/link_values.py, plan_link_values).
            class Walk(schema.as_state_machine()):  # type: ignore[misc]  # a class Schemathesis builds
                def setup(self) -> None:
                    self.scenario = transaction.atomic()
                    self.scenario.__enter__()

                def teardown(self) -> None:
                    transaction.set_rollback(True)
                    self.scenario.__exit__(None, None, None)

                def before_call(self, case: Any) -> None:
                    # A new key for each change, as a client sends it: the walk keeps what each step wrote.
                    if case.method.upper() == "POST":
                        case.headers = {**(case.headers or {}), IDEMPOTENCY_HEADER: uuid.uuid4().hex}

                def get_call_kwargs(self, case: Any) -> dict[str, Any]:
                    return {"headers": headers, "environ_overrides": LOOPBACK}

                def validate_response(self, response: Any, case: Any, additional_checks=None, **kwargs: Any) -> None:
                    case.validate_response(
                        response, checks=CHECKS, transport_kwargs=kwargs or None, recorder=self.recorder
                    )

            run_state_machine_as_test(
                Walk,
                settings=settings(
                    max_examples=SCENARIOS,
                    stateful_step_count=STEPS,
                    derandomize=True,
                    database=None,
                    deadline=None,
                    suppress_health_check=list(HealthCheck),
                ),
            )
