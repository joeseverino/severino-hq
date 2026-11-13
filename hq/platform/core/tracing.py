"""Each request's id, and spans for a trace viewer on a developer's machine.

The id is HQ's own: minted here for every request a door serves, carried on
every log line and as the operation id of every audit row the request writes,
and never taken from the caller. While ``OTEL_EXPORTER_OTLP_ENDPOINT`` names
a collector (docs/Development.md, "Tracing"), the id is also the W3C trace id
of the request's trace, and ``span`` opens a span in it at each chokepoint.
Unset, as in every image, which installs no SDK, ``span`` is a null context
and nothing of OpenTelemetry is loaded.

Spans are telemetry, which a viewer drops or expires; they carry no actor's
name, and no audit row depends on one.
"""

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import Context, ContextVar
from functools import cache
from typing import Any
from uuid import uuid7

lazy from opentelemetry import context as otel_context, trace as otel_trace
lazy from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
lazy from opentelemetry.sdk.resources import Resource
lazy from opentelemetry.sdk.trace import TracerProvider
lazy from opentelemetry.sdk.trace.export import BatchSpanProcessor
lazy from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

# The standard variable the OTLP exporter reads its collector's address from.
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
# What a line logged outside every request carries as its id.
NO_TRACE = "-"

_trace_id = ContextVar("hq_trace_id", default=NO_TRACE)


def trace_id() -> str:
    """The id of the request this context serves, or ``NO_TRACE``."""

    return _trace_id.get()


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
