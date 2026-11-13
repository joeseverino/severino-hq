"""The worker's spans, for a trace viewer on a developer's machine: none
unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` names a collector
(docs/Development.md, "Tracing").

Its own, since the worker imports nothing of HQ, and like HQ's: unset, as in
the worker's image, which installs no SDK, each call does nothing and nothing
of OpenTelemetry is loaded. A span carries the run's operation, procedure and
outcomes, and never who asked for it or its parameters.
"""

import os
from contextlib import AbstractContextManager, nullcontext
from functools import cache
from typing import Any

lazy from opentelemetry import trace as otel_trace
lazy from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
lazy from opentelemetry.sdk.resources import Resource
lazy from opentelemetry.sdk.trace import TracerProvider
lazy from opentelemetry.sdk.trace.export import BatchSpanProcessor

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"


def span(name: str, **attributes: Any) -> AbstractContextManager[Any]:
    """A span around the block, which `as` names; a null context naming None unless spans are sent."""

    sending = tracer()
    return nullcontext() if sending is None else sending.start_as_current_span(name, attributes=attributes)


def note(**attributes: Any) -> None:
    """Set ``attributes`` on the current span."""

    if tracer() is not None:
        otel_trace.get_current_span().set_attributes(attributes)


def event(span: Any, name: str, **attributes: Any) -> None:
    """Add an event to ``span``, the one a ``span`` block opened: ansible-runner
    calls back from a thread of its own, where no span is current."""

    if span is not None:
        span.add_event(name, attributes)


@cache
def tracer() -> Any:
    if not os.environ.get(ENDPOINT):
        return None
    provider = TracerProvider(resource=Resource.create({"service.name": "hq-runner"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    otel_trace.set_tracer_provider(provider)
    return provider.get_tracer("hq_runner")
