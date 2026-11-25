"""Each request's id, each chain's, and spans for a trace viewer on a developer's machine (docs/Development.md, "Tracing").

The id is HQ's own, minted for every request and never taken from the caller.
A chain of operations (a press, the work it queued, the work that queued) is
one trace: its id is the root operation's, in W3C Trace Context's shape, and
every audit row of the chain carries it (``AuditLog.trace``). Queued work
carries its creation context as a ``traceparent``, as OpenTelemetry's
messaging conventions attach one to a message, and the work that takes it up
continues that trace.

Unset, as in every image, ``span`` is a null context and nothing of
OpenTelemetry loads. Spans carry no actor's name, and no audit row depends on
one: the audit row holds its chain alone, and spans only join it.
"""

import os
import re
import secrets
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import Context, ContextVar
from functools import cache
from hashlib import blake2s
from typing import Any
from uuid import UUID, uuid7

lazy from opentelemetry import context as otel_context, trace as otel_trace
lazy from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
lazy from opentelemetry.sdk.resources import Resource
lazy from opentelemetry.sdk.trace import TracerProvider
lazy from opentelemetry.sdk.trace.export import BatchSpanProcessor
lazy from opentelemetry.sdk.trace.id_generator import RandomIdGenerator
lazy from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

# The standard variable the OTLP exporter reads its collector's address from.
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
# What a line logged outside every request carries as its id.
NO_TRACE = "-"
# W3C Trace Context's trace-id, and a version-00 traceparent's four fields.
_TRACE_ID = re.compile(r"[0-9a-f]{32}")
_TRACEPARENT = re.compile(r"00-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}")

_trace_id = ContextVar("hq_trace_id", default=NO_TRACE)


def trace_id() -> str:
    """The id of the request this context serves, or ``NO_TRACE``."""

    return _trace_id.get()


def chain_id(operation_id: str) -> str:
    """The trace id of a chain whose root is ``operation_id``: W3C's 32
    lowercase hex digits, which a request's id already is, a uuid's digits,
    else a digest of the id, so every row of one root names the same chain."""

    if not operation_id or _TRACE_ID.fullmatch(operation_id):
        return operation_id
    try:
        return UUID(operation_id).hex
    except ValueError:
        return blake2s(operation_id.encode(), digest_size=16).hexdigest()


def traceparent(chain: str) -> str:
    """The creation context of a message made now in ``chain`` (W3C
    ``traceparent``): the current span's while spans are sent and it is in
    that chain, else an unsampled parent of the chain, as a caller that
    records no span sends one."""

    if not chain:
        return ""
    if tracer() is not None:
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        if (sent := carrier.get("traceparent", "")).split("-")[1:2] == [chain]:
            return sent
    return f"00-{chain}-{secrets.token_hex(8)}-00"


def chain_of(carried: str) -> str:
    """The chain a ``traceparent`` names, or "" for none."""

    match = _TRACEPARENT.fullmatch(carried)
    return match[1] if match else ""


def continued(carried: str, name: str) -> AbstractContextManager[Any]:
    """A span named ``name`` around the block, parented by the creation
    context ``carried``: work taken off the queue continues the trace that
    queued it. A null context unless spans are sent."""

    sending = tracer()
    if sending is None or not carried:
        return nullcontext()
    parent = TraceContextTextMapPropagator().extract({"traceparent": carried})
    return sending.start_as_current_span(name, context=parent, links=_links())


def _links() -> list[Any]:
    """A link to the span the block runs in, the request that took the work up."""

    current = otel_trace.get_current_span().get_span_context()
    return [otel_trace.Link(current)] if current.is_valid else []


@contextmanager
def trace(name: str, **attributes: Any) -> Iterator[str]:
    """Serve one request as a trace of its own: mint its id, carry it for the
    block, open its root span, and yield the id."""

    minted = uuid7().hex
    with _trace_id.set(minted), _root(name, attributes):
        yield minted


def span(name: str, **attributes: Any) -> AbstractContextManager[Any]:
    """A span named ``name`` around the block, a child of the current one;
    a null context unless spans are sent."""

    sending = tracer()
    if sending is None:
        return nullcontext()
    return sending.start_as_current_span(name, attributes=attributes)


def note(**attributes: Any) -> None:
    """Set ``attributes`` on the current span, once the block has learned them."""

    if tracer() is not None:
        otel_trace.get_current_span().set_attributes(attributes)


def _root(name: str, attributes: dict[str, Any]) -> AbstractContextManager[Any]:
    sending = tracer()
    if sending is None:
        return nullcontext()
    # An empty parent, so the request's id is the trace's whatever the caller
    # sent: a span of another trace never adopts it.
    return sending.start_as_current_span(name, context=otel_context.Context(), attributes=attributes)


@cache
def tracer() -> Any:
    """The tracer spans are sent through, or None while no endpoint is set."""

    if not os.environ.get(ENDPOINT):
        return None
    # Started in an empty context, so its export thread serves no request
    # however the interpreter starts threads: the outbound hook refuses any
    # connection made while one is served (core.outbound).
    sent = Context().run(lambda: provider(BatchSpanProcessor(OTLPSpanExporter())))
    otel_trace.set_tracer_provider(sent)
    return sent.get_tracer("hq")


def provider(processor: Any) -> Any:
    """A tracer provider handing each span that ends to ``processor``, whose
    root spans take their request's id as their trace id."""

    made = TracerProvider(resource=Resource.create({"service.name": "hq"}), id_generator=_request_ids())
    made.add_span_processor(processor)
    return made


@cache
def _request_ids() -> Any:
    """An id generator whose root span's trace id is its request's: a uuid7's
    62 low bits are random, which the W3C random flag asks of 56. Its class is
    made on first use, since a base class is read as its module loads."""

    class RequestIds(RandomIdGenerator):
        def generate_trace_id(self) -> int:
            current = _trace_id.get()
            return super().generate_trace_id() if current == NO_TRACE else int(current, 16)

    return RequestIds()
