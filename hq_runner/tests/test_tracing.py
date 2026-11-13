"""A pass of the worker is one trace from its claim to its report, carrying
what ran and how each task ended and never who asked or with what; with no
endpoint nothing of OpenTelemetry loads."""

import json
import os
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

from . import fakes
from .test_worker import PassTestCase

try:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:  # pragma: no cover - the worker's image carries no SDK
    SDK = False
else:
    SDK = True

ROOT = Path(__file__).resolve().parents[2]
ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
UNTRACED = """
import json, sys
from hq_runner import tracing
with tracing.span("automation run", example="yes") as ran:
    tracing.note(outcome="ok")
    tracing.event(ran, "lease renewed")
print(json.dumps(sorted(name for name in sys.modules if name.startswith("opentelemetry"))))
"""


class UntracedTests(unittest.TestCase):
    def test_with_no_endpoint_a_pass_loads_nothing_of_opentelemetry(self):
        env = {key: value for key, value in os.environ.items() if key != ENDPOINT}
        result = subprocess.run(
            [sys.executable, "-c", UNTRACED], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=True
        )

        self.assertEqual(json.loads(result.stdout), [])


def threaded(runner):
    """``runner``, calling back from a thread of its own as ansible-runner's run_async does."""

    def run(run, *, on_event, should_stop):
        ended = []
        thread = threading.Thread(target=lambda: ended.append(runner(run, on_event=on_event, should_stop=should_stop)))
        thread.start()
        thread.join()
        return ended[0]

    return run


@unittest.skipUnless(SDK, "the worker's image installs no OpenTelemetry SDK")
class TracedTests(PassTestCase):
    def setUp(self):
        super().setUp()
        from hq_runner import tracing

        self.sent = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.sent))
        self.enterContext(mock.patch.object(tracing, "tracer", return_value=provider.get_tracer("test")))

    def test_a_pass_is_one_trace_with_each_task_outcome_and_none_of_its_asker_or_parameters(self):
        secret = "example-not-a-real-secret-value"
        self.work(fakes.Bridge(fakes.pending(parameters={"token": secret})), threaded(fakes.Runner(fakes.WELL)))

        spans = self.sent.get_finished_spans()
        (root,) = [span for span in spans if span.parent is None]
        (ran,) = [span for span in spans if span.parent is not None]
        self.assertEqual(ran.parent.span_id, root.context.span_id)
        self.assertEqual(root.attributes["hq.operation.id"], fakes.OPERATION)
        self.assertTrue(root.attributes["hq.ok"])
        self.assertEqual(ran.attributes["hq.procedure"], fakes.PROCEDURE)
        self.assertEqual(
            [(event.name, event.attributes["ansible.task"]) for event in ran.events],
            [("ok", "Say hello"), ("changed", "Touch a file"), ("ok", "Hidden"), ("skipped", "Skip me")],
        )
        said = json.dumps(
            [dict(span.attributes) for span in spans] + [dict(e.attributes) for s in spans for e in s.events]
        )
        self.assertNotIn("example-agent", said)
        self.assertNotIn(secret, said)
