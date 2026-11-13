"""A request is one trace under its own id, and with no endpoint nothing of OpenTelemetry loads."""

import json
import subprocess
import sys
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from hq.platform.application.agent_access import SET_AGENT_ACCESS
from hq.platform.application.plugin_testing import form_keyed
from hq.platform.core.tests.test_settings_env import REPO_ROOT, subprocess_env

try:
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:  # pragma: no cover - the image does not carry the SDK
    SDK = False
else:
    SDK = True

ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
UNTRACED = """
import json, sys
from hq.platform.core import tracing
with tracing.trace("GET /example/") as minted:
    with tracing.span("capability example.read", example="yes"):
        tracing.note(outcome="ok")
    inside = tracing.trace_id()
print(json.dumps({"minted": minted, "inside": inside, "after": tracing.trace_id(),
                  "loaded": sorted(name for name in sys.modules if name.startswith("opentelemetry"))}))
"""


class UntracedTests(SimpleTestCase):
    def test_with_no_endpoint_a_traced_request_loads_nothing_of_opentelemetry(self):
        result = subprocess.run(
            [sys.executable, "-c", UNTRACED],
            cwd=REPO_ROOT,
            env=subprocess_env(drop=(ENDPOINT,)),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        ran = json.loads(result.stdout)

        self.assertEqual(ran["loaded"], [])
        self.assertEqual(ran["inside"], ran["minted"])
        self.assertEqual(ran["after"], "-")


# A span ended while a request is served, under the lazy imports every HQ
# process runs with: what its export thread was serving when it sent it, and
# whether the batch encoded as OTLP's protobuf.
EXPORTED = """
from hq.config.imports import lazily
lazily()
import threading
from hq.platform.core import outbound, tracing
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

sent = threading.Event()
seen = []

class Recording(InMemorySpanExporter):
    def export(self, spans):
        try:
            encode_spans(spans).SerializeToString()
        except Exception as exc:
            encoded = repr(exc)
        else:
            encoded = "encoded"
        seen.append((outbound.serving_request(), encoded))
        sent.set()
        return super().export(spans)

class Request:
    method, path = "GET", "/example/"

tracing.OTLPSpanExporter = Recording
with outbound.serving(Request()), tracing.span("capability example.read"):
    pass
assert sent.wait(30), "nothing was exported"
print(repr(seen[0]))
"""


@skipUnless(SDK, "the image installs no OpenTelemetry SDK")
class ExportThreadTests(SimpleTestCase):
    def test_the_export_thread_serves_no_request_and_encodes_however_the_process_imports(self):
        for inherit in ("0", "1"):
            with self.subTest(thread_inherit_context=inherit):
                result = subprocess.run(
                    [sys.executable, "-X", f"thread_inherit_context={inherit}", "-c", EXPORTED],
                    cwd=REPO_ROOT,
                    env=subprocess_env(**{ENDPOINT: "http://127.0.0.1:9", "OTEL_BSP_SCHEDULE_DELAY": "1"}),
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=True,
                )

                self.assertEqual(result.stdout.strip(), repr(("", "encoded")))


@skipUnless(SDK, "the image installs no OpenTelemetry SDK")
class TracedTests(TestCase):
    def setUp(self):
        from hq.platform.core import tracing

        self.sent = InMemorySpanExporter()
        tracer = tracing.provider(SimpleSpanProcessor(self.sent)).get_tracer("test")
        self.enterContext(mock.patch.object(tracing, "tracer", return_value=tracer))

    def test_a_web_request_is_one_trace_under_its_request_id_with_a_span_for_its_capability(self):
        user = get_user_model().objects.create_user("example-operator", password="x" * 20)
        self.client.force_login(user)

        response = self.client.post(reverse("agent_access"), form_keyed({"paused": "1"}))

        spans = self.sent.get_finished_spans()
        (root,) = [span for span in spans if span.parent is None]
        (capability,) = [span for span in spans if span.attributes.get("hq.capability") == SET_AGENT_ACCESS]
        self.assertEqual(format(root.context.trace_id, "032x"), response.wsgi_request.request_id)
        self.assertEqual({span.context.trace_id for span in spans}, {root.context.trace_id})
        self.assertEqual(root.attributes["http.response.status_code"], response.status_code)
        self.assertTrue(capability.attributes["hq.ok"])
        self.assertEqual(capability.attributes["hq.interface"], "web")
        said = {str(value) for span in spans for value in span.attributes.values()}
        self.assertNotIn(user.get_username(), said)

    def test_a_request_never_joins_the_trace_it_arrived_in(self):
        from hq.platform.core import tracing

        with tracing.span("outer"), tracing.trace("GET /example/") as minted:
            pass

        root, outer = sorted(self.sent.get_finished_spans(), key=lambda span: span.name)
        self.assertIsNone(root.parent)
        self.assertEqual(format(root.context.trace_id, "032x"), minted)
        self.assertNotEqual(root.context.trace_id, outer.context.trace_id)

    def test_an_mcp_request_is_one_trace_whose_id_its_audit_rows_carry(self):
        from asgiref.sync import async_to_sync, sync_to_async

        from hq.platform.application.capabilities import run_capability
        from hq.platform.application.security import Capability, Principal
        from hq.platform.core.attribution import BoundDoor
        from hq.platform.core.models import AuditLog

        granted = frozenset(Capability)
        agent = Principal("example-agent", "mcp", granted, granted=granted)

        async def runs(_scope, _receive, send):
            await sync_to_async(run_capability)(SET_AGENT_ACCESS, {"paused": True}, principal=agent)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        async def call():
            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(_message):
                pass

            scope = {"type": "http", "method": "POST", "path": "/mcp/", "headers": []}
            await BoundDoor(runs, interface="mcp", operation="mcp")(scope, receive, send)

        async_to_sync(call)()

        spans = self.sent.get_finished_spans()
        (root,) = [span for span in spans if span.parent is None]
        (capability,) = [span for span in spans if span.parent is not None]
        denied = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertEqual(format(root.context.trace_id, "032x"), denied.operation_id)
        self.assertEqual(capability.parent.span_id, root.context.span_id)
        self.assertEqual(capability.attributes["error.type"], "forbidden")
        said = {str(value) for span in spans for value in span.attributes.values()}
        self.assertNotIn("example-agent", said)
