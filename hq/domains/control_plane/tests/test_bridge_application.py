"""The bridge application: what it serves, what it refuses, and where it can be reached from."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import Resolver404, resolve
from jsonschema import Draft202012Validator

from hq.domains.control_plane import bridge_application
from hq.domains.control_plane.bridge_actions import ACTIONS, Action
from hq.domains.control_plane.bridge_contract import contract, max_body_bytes, operations

from . import bridge_client

ROOT = Path(settings.BASE_DIR)
PROBLEM = contract()["components"]["schemas"]["Problem"]


class RefusalTests(TestCase):
    """A call the contract does not describe is refused as a problem, and runs nothing."""

    def refused(self, status: int, detail: str, path: str, **request) -> None:
        answer = bridge_client.request(path, **request)
        self.assertEqual(answer.status, status, answer.body)
        self.assertEqual(answer.media_type, "application/problem+json")
        problem = answer.json()
        Draft202012Validator(PROBLEM).validate(problem)
        self.assertEqual(problem["status"], status)
        self.assertIn(detail, problem["detail"])

    def test_an_action_the_contract_does_not_name(self):
        self.refused(404, "no such action", "/destroy")
        self.refused(404, "no such action", "/claim/extra")
        self.refused(404, "no such action", "/")

    def test_a_method_other_than_post(self):
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            with self.subTest(method=method):
                self.refused(405, "Method Not Allowed", "/registry", method=method)

    def test_a_missing_required_parameter(self):
        self.refused(400, "controller-id is required", "/claim")
        self.refused(400, "resource is required", "/export")

    def test_a_parameter_the_action_does_not_take(self):
        self.refused(400, "registry takes no force", "/registry", query={"force": "1"})
        self.refused(400, "peek takes no controller-id", "/peek", query={"controller-id": "x"})

    def test_a_parameter_given_twice(self):
        self.refused(400, "given more than once", "/schedule", query={"controller-id": ["a", "b"]})

    def test_a_lease_outside_the_contracts_range(self):
        for lease in ("29", "3601", "-1", "soon", "1.5"):
            with self.subTest(lease=lease):
                self.refused(400, "lease-seconds must be", "/claim", query={"controller-id": "x", "lease-seconds": lease})

    def test_a_lease_inside_the_range_and_the_contracts_default(self):
        with patch("hq.domains.control_plane.bridge_actions.claim_next_operation", return_value={"ok": True}) as claim:
            bridge_client.call("claim", controller_id="x", lease_seconds="45", capability=["a:b", "c:d"])
            bridge_client.call("claim", controller_id="x")
        self.assertEqual(claim.call_args_list[0].kwargs, {"lease_seconds": 45, "capabilities": (("a", "b"), ("c", "d"))})
        self.assertEqual(claim.call_args_list[1].kwargs, {"lease_seconds": 300, "capabilities": ()})

    def test_a_malformed_capability(self):
        self.refused(400, "kind:action", "/peek", query={"capability": "no-colon"})

    def test_a_payload_on_an_action_that_takes_none(self):
        self.refused(400, "registry takes no payload", "/registry", body=b"{}")

    def test_a_payload_that_is_not_json(self):
        for body in (b"", b"not json", b'{"open": '):
            with self.subTest(body=body):
                self.refused(400, "not JSON", "/steps", query={"controller-id": "x"}, body=body)

    def test_input_an_action_will_not_take(self):
        self.refused(400, "Managed resource was not found", "/export", query={"resource": "absent"})
        self.refused(400, "", "/report", query={"controller-id": "x", "operation": "1"}, body=b'{"success": "maybe"}')

    def test_a_payload_over_the_limit_is_refused_before_it_is_read_or_run(self):
        ran = []
        with (
            patch.object(bridge_application, "max_body_bytes", return_value=8),
            patch.object(bridge_application, "_run", side_effect=lambda *args: ran.append(args)),
        ):
            # Declared too large, and too large without declaring it.
            self.refused(413, "larger than the bridge accepts", "/steps", query={"controller-id": "x"},
                         body=b"[]", headers=((b"content-length", b"9"),))
            self.refused(413, "larger than the bridge accepts", "/steps", query={"controller-id": "x"}, body=b"[" + b" " * 8 + b"]")
        self.assertEqual(ran, [])

    def test_an_answer_over_the_limit_is_not_sent(self):
        with patch.object(bridge_application, "max_body_bytes", return_value=8):
            self.refused(500, "larger than the bridge sends", "/registry")

    def test_an_action_that_fails_says_what_failed_and_no_more(self):
        with (
            patch("hq.domains.control_plane.bridge_actions.controller_registry", side_effect=RuntimeError("x" * 2000)),
            self.assertLogs("severino.bridge", "ERROR"),
        ):
            answer = bridge_client.request("/registry")
        self.assertEqual(answer.status, 500)
        detail = answer.json()["detail"]
        self.assertTrue(detail.startswith("RuntimeError: x"))
        self.assertEqual(len(detail), bridge_application.DETAIL_LIMIT)
        self.assertNotIn("Traceback", detail)

    def test_the_limit_is_the_contracts(self):
        self.assertEqual(max_body_bytes(), 64 * 1024 * 1024)


class ContractBindingTests(SimpleTestCase):
    def test_the_routes_are_the_contracts_paths(self):
        served = {route.path for route in bridge_application.application.application.routes}
        self.assertEqual(served, set(contract()["paths"]))
        for route in bridge_application.application.application.routes:
            self.assertEqual(route.methods, {"POST"})

    def test_actions_and_paths_that_differ_stop_the_import(self):
        extra = (*ACTIONS, Action("undeclared", lambda parameters, payload: None))
        for actions in (extra, ACTIONS[1:], (*ACTIONS, ACTIONS[0])):
            with self.subTest(count=len(actions)), patch.object(bridge_application, "ACTIONS", actions):
                with self.assertRaisesMessage(RuntimeError, "differ"):
                    bridge_application._routes()

    def test_every_parameter_is_a_kind_the_application_parses(self):
        for operation in operations().values():
            for parameter in operation.parameters:
                with self.subTest(action=operation.name, parameter=parameter.name):
                    if parameter.repeated:
                        self.assertEqual(parameter.schema["items"], {"type": "string"})
                    else:
                        self.assertIn(parameter.schema["type"], {"string", "integer"})


class ReachabilityTests(TestCase):
    """The bridge answers on its Unix socket and nowhere else; the web answers no bridge action."""

    def test_a_request_from_a_network_listener_is_refused_before_any_action(self):
        with patch.object(bridge_application, "_run") as run:
            for server in (("127.0.0.1", 8000), ("0.0.0.0", 8000), ("::1", 8000), None):
                for action in operations():
                    with self.subTest(server=server, action=action):
                        answer = bridge_client.request(f"/{action}", query={"controller-id": "x"}, server=server)
                        self.assertEqual(answer.status, 403)
                        self.assertIn("Unix socket only", answer.json()["detail"])
        run.assert_not_called()

    def test_the_web_application_serves_no_bridge_action(self):
        with patch.object(bridge_application, "_run") as run:
            for action, operation in operations().items():
                with self.subTest(action=action):
                    response = self.client.post(
                        f"/{action}?controller-id=x&resource=x&operation=1",
                        data=b"[]" if operation.takes_body else b"",
                        content_type="application/json",
                    )
                    self.assertNotEqual(response.status_code, 200)
                    self.assertNotEqual(response.headers.get("Content-Type"), "application/problem+json")
        run.assert_not_called()

    def test_no_web_route_is_a_bridge_action(self):
        from hq.config.asgi import application as web

        for action in operations():
            with self.subTest(action=action), self.assertRaises(Resolver404):
                resolve(f"/{action}")
        mounted = [getattr(route, "app", None) for route in web.routes]
        self.assertNotIn(bridge_application.application, mounted)
        self.assertNotIn(bridge_application.application.application, mounted)

    def test_the_bridge_serves_no_web_route(self):
        for path in ("/health/ready/", "/health/live/", "/api/v2/openapi.json", "/admin/", "/mcp", "/static/app.css"):
            for method in ("GET", "POST"):
                with self.subTest(path=path, method=method):
                    self.assertEqual(bridge_client.request(path, method=method).status, 404)

    def test_only_the_socket_listener_is_given_the_bridge_application(self):
        """Source rule: one module hands the application to a listener, and it is the Unix one."""

        mention = re.compile(r"\bbridge_application\b")
        holders = sorted(
            str(path.relative_to(ROOT))
            for path in (ROOT / "hq").rglob("*.py")
            if "tests" not in path.parts
            and path.name != "bridge_application.py"
            and mention.search(path.read_text(encoding="utf-8"))
        )
        self.assertEqual(holders, ["hq/config/asgi.py"])
        source = (ROOT / "hq/config/asgi.py").read_text(encoding="utf-8")
        uses = [line.strip() for line in source.splitlines() if mention.search(line) and not line.lstrip().startswith("#")]
        self.assertEqual(
            uses,
            [
                "from hq.domains.control_plane.bridge_application import application as bridge_application",
                "async with serving(bridge_application, settings.SEVERINO_BRIDGE_SOCKET):",
            ],
        )

    def test_without_a_socket_named_no_bridge_is_served(self):
        import asyncio

        from hq.config import asgi

        async def entered() -> bool:
            with patch("hq.platform.core.unix_server.serving") as serving:
                async with asgi.bridge_serving():
                    return serving.called

        with self.settings(SEVERINO_BRIDGE_SOCKET=""):
            self.assertFalse(asyncio.run(entered()))

    def test_a_request_is_json_and_sorted_as_the_controller_reads_it(self):
        answer = bridge_client.request("/registry")
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.media_type, "application/json")
        self.assertEqual(answer.body, json.dumps(answer.json(), sort_keys=True).encode())
