"""Every door binds who acted where it starts, and a row written past every door is refused.

The test runner binds each test's own thread as the test (interface "test", no
operation id), so a row a door wrote is told from the test's by carrying the
door's interface and an id of its own.
"""

import threading
from datetime import timedelta
from unittest import mock

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from hq.domains.control_plane import bridge_application
from hq.domains.control_plane.bridge_actions import ACTIONS, Action
from hq.domains.control_plane.tests import bridge_client
from hq.domains.jobs.runner import start
from hq.platform.api.tests.tokens import ISSUER, RESOURCE, _serving, _token
from hq.platform.core.audit import audit_operation, operation_context, record_event, record_operation
from hq.platform.core.models import AuditLog

PASSWORD = "correct-horse-battery"


def _example_row(message: str) -> None:
    record_operation("example.door", message, action=AuditLog.Action.UPDATED)


def _row(message: str) -> AuditLog:
    return AuditLog.objects.get(object_type="example.door", message=message)


class RequestDoorTests(TestCase):
    def setUp(self):
        get_user_model().objects.create_user(username="joe", password=PASSWORD)

    def test_a_web_request_is_one_operation_under_its_request_id_by_whoever_it_signed_in(self):
        failed = self.client.post("/accounts/login/", {"username": "joe", "password": "wrong"})
        signed_in = self.client.post("/accounts/login/", {"username": "joe", "password": PASSWORD})

        refused = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        login = AuditLog.objects.get(action=AuditLog.Action.LOGIN)
        self.assertEqual(refused.operation_id, failed["X-Request-ID"])
        self.assertEqual(login.operation_id, signed_in["X-Request-ID"])
        self.assertEqual(refused.metadata["interface"], "web")
        self.assertEqual(login.metadata["interface"], "web")
        # Nobody had signed in when the attempt failed; joe had once it succeeded.
        self.assertEqual(refused.metadata["actor"], "anonymous")
        self.assertEqual(login.metadata["actor"], "joe")
        self.assertEqual(login.metadata["operation"], refused.metadata["operation"])

    @override_settings(OIDC_ISSUER=ISSUER, SEVERINO_API_RESOURCE=RESOURCE, OIDC_RP_SIGN_ALGO="RS256")
    def test_an_api_request_is_the_apis_operation_under_its_request_id(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='')}")

        denied = AuditLog.objects.get(action=AuditLog.Action.DENIED)
        self.assertEqual(denied.metadata["interface"], "api")
        self.assertEqual(denied.operation_id, response["X-Request-ID"])


class MCPDoorTests(TestCase):
    def test_an_mcp_request_is_the_mcp_doors_operation(self):
        from hq.config import asgi
        from hq.platform.mcp.security import MCPBoundary

        async def writes(_boundary, scope, receive, send):
            await sync_to_async(_example_row)("through mcp")
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/mcp/",
            "raw_path": b"/api/mcp/",
            "root_path": "",
            "query_string": b"",
            "headers": [(b"host", b"hq.example.test")],
            "client": ("100.64.0.10", 12345),
            "server": ("100.64.0.20", 8000),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        with mock.patch.object(MCPBoundary, "__call__", writes):
            async_to_sync(asgi.application)(scope, receive, send)

        row = _row("through mcp")
        self.assertEqual(row.metadata["interface"], "mcp")
        self.assertNotEqual(row.operation_id, "")


class MCPAdmittedCallerTests(TestCase):
    def test_a_refusal_after_admission_names_the_agent(self):
        from hq.platform.core.attribution import BoundDoor
        from hq.platform.mcp.security import MCPBoundary, TailnetGate
        from hq.platform.mcp.tests.test_mcp import RESOURCE as MCP_RESOURCE, TOKEN, _accept_the_example_token, admitting

        async def refuses(_scope, _receive, send):
            # Written outside any capability, as a refusal at the door is.
            await sync_to_async(record_event)(
                action=AuditLog.Action.DENIED, type_label="example.door", message="refused"
            )
            await send({"type": "http.response.start", "status": 403, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        door = BoundDoor(
            MCPBoundary(
                refuses,
                gate=TailnetGate(allowed_hosts=("a-docker-host",), allowed_networks=("100.64.0.0/10",)),
                resource=MCP_RESOURCE,
                admit=admitting(_accept_the_example_token),
            ),
            interface="mcp",
            operation="mcp",
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"host", b"a-docker-host"), (b"authorization", f"Bearer {TOKEN}".encode())],
            "client": ("100.64.0.10", 12345),
            "server": ("100.64.0.20", 8000),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(_message):
            return None

        async_to_sync(door.__call__)(scope, receive, send)

        row = _row("refused")
        self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("mcp", "example-agent"))
        self.assertEqual(row.metadata["operation"], "mcp")
        self.assertNotEqual(row.operation_id, "")


class BridgeDoorTests(TestCase):
    def test_a_controller_call_is_the_controllers_operation(self):
        def writes(_parameters, _payload):
            _example_row("through the bridge")

        app = bridge_application.bridge(
            tuple(Action(action.name, writes if action.name == "registry" else action.run) for action in ACTIONS)
        )

        answer = bridge_client.request("/registry", app=app)

        self.assertEqual(answer.status, 200, answer.body)
        row = _row("through the bridge")
        self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("controller", "controller"))
        self.assertNotEqual(row.operation_id, "")


class CommandDoorTests(TestCase):
    def test_a_management_command_is_the_local_operators(self):
        import manage

        def command(_argv):
            _example_row("through the shell")

        with (
            mock.patch.object(manage, "execute_from_command_line", command),
            mock.patch.object(manage.sys, "argv", ["manage.py", "example_command"]),
        ):
            manage.main()

        row = _row("through the shell")
        self.assertEqual((row.metadata["interface"], row.metadata["actor"]), ("cli", "local-operator"))
        self.assertIn("example_command", row.metadata["operation"])
        self.assertNotEqual(row.operation_id, "")


class JobDoorTests(TransactionTestCase):
    """TransactionTestCase because the work runs on a thread of its own."""

    def test_a_jobs_work_is_the_jobs_operation(self):
        job = start("example.door", "Example", lambda _progress: _example_row("through a job") or {})
        deadline = timezone.now() + timedelta(seconds=10)
        while job.is_live and timezone.now() < deadline:
            job.refresh_from_db()

        row = _row("through a job")
        self.assertEqual(row.metadata["interface"], "job")
        self.assertEqual(row.operation_id, str(job.pk))


class UnboundTests(TestCase):
    def _on_a_thread_of_its_own(self, write) -> BaseException | None:
        raised: list[BaseException] = []

        def run():
            try:
                write()
            except Exception as exc:  # noqa: BLE001 - the refusal is what is read
                raised.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        return raised[0] if raised else None

    def test_a_row_written_where_no_door_bound_is_refused(self):
        refused = self._on_a_thread_of_its_own(lambda: record_event(action=AuditLog.Action.UPDATED, message="x"))

        self.assertIsInstance(refused, RuntimeError)
        self.assertIn("no operation bound", str(refused))

    def test_an_operation_with_no_principal_and_nobody_bound_is_refused(self):
        def begin():
            with audit_operation(operation="example.nobody"):
                pass

        self.assertIsInstance(self._on_a_thread_of_its_own(begin), RuntimeError)

    def test_an_operation_with_no_principal_is_whoever_is_bound(self):
        with (
            operation_context(interface="cli", actor="example-operator", operation="example.outer"),
            audit_operation(operation="example.inner"),
        ):
            _example_row("inherited")

        row = _row("inherited")
        self.assertEqual(
            (row.metadata["interface"], row.metadata["actor"], row.metadata["operation"]),
            ("cli", "example-operator", "example.inner"),
        )
