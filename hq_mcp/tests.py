from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from application import projection
from assets.models import Asset
from docs_index.models import DocumentationRecord
from projects.models import Project

from . import services
from .server import mcp
from .security import MCPBoundary

TOKEN = "a" * 48


class SecretSettingsTests(SimpleTestCase):
    def test_secret_can_be_loaded_from_file(self):
        from config.settings import env_secret

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text(f"{TOKEN}\n", encoding="utf-8")
            from unittest.mock import patch

            with patch.dict(
                "os.environ",
                {"TEST_MCP_TOKEN_FILE": str(path)},
                clear=False,
            ):
                self.assertEqual(env_secret("TEST_MCP_TOKEN"), TOKEN)

    def test_secret_rejects_file_and_environment_value_together(self):
        from config.settings import env_secret
        from unittest.mock import patch

        with patch.dict(
            "os.environ",
            {
                "TEST_MCP_TOKEN": TOKEN,
                "TEST_MCP_TOKEN_FILE": "/unused",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(RuntimeError, "Set only one"):
                env_secret("TEST_MCP_TOKEN")


class ServiceTests(TestCase):
    def test_registered_tools_are_async_safe(self):
        async def call_health():
            tool = mcp._tool_manager.get_tool("system_health")
            return await tool.run({})

        result = async_to_sync(call_health)()

        self.assertEqual(result["status"], "ok")

    def test_project_detail_returns_safe_relationships_only(self):
        project = Project.objects.create(
            name="HQ MCP", technologies_used="Django, MCP"
        )
        asset = Asset.objects.create(item_name="Lab server")
        asset.related_projects.add(project)
        safe_doc = DocumentationRecord.objects.create(
            doc_id="rb-hq-mcp",
            title="Use HQ MCP",
            sensitivity=DocumentationRecord.Sensitivity.INTERNAL,
        )
        restricted_doc = DocumentationRecord.objects.create(
            doc_id="rb-hq-mcp-token",
            title="HQ MCP token",
            sensitivity=DocumentationRecord.Sensitivity.RESTRICTED,
        )
        safe_doc.related_projects.add(project)
        restricted_doc.related_projects.add(project)

        result = services.get_project(project.slug)

        self.assertEqual(result["technologies"], ["Django", "MCP"])
        self.assertEqual(result["relationships"]["assets"], [asset.slug])
        self.assertEqual(
            result["relationships"]["documentation"], ["rb-hq-mcp"]
        )

    def test_documentation_status_excludes_sensitive_and_restricted_records(self):
        DocumentationRecord.objects.create(
            doc_id="rb-safe",
            title="Safe",
            sensitivity=DocumentationRecord.Sensitivity.INTERNAL,
        )
        DocumentationRecord.objects.create(
            doc_id="rb-sensitive",
            title="Sensitive",
            sensitivity=DocumentationRecord.Sensitivity.SENSITIVE,
        )
        DocumentationRecord.objects.create(
            doc_id="rb-restricted",
            title="Restricted",
            sensitivity=DocumentationRecord.Sensitivity.RESTRICTED,
        )

        result = services.documentation_status()

        self.assertEqual(result["total"], 1)
        self.assertEqual(
            [record["doc_id"] for record in result["records"]], ["rb-safe"]
        )

    def test_page_size_is_bounded(self):
        for number in range(105):
            Project.objects.create(name=f"Project {number:03d}")

        result = services.list_projects(limit=500)

        self.assertEqual(result["count"], projection.MAX_PAGE_SIZE)

    def test_missing_object_uses_structured_service_error(self):
        with self.assertRaisesRegex(services.NotFoundError, "was not found"):
            services.get_asset("missing")

    def test_registry_audit_is_a_registered_read_tool(self):
        Project.objects.create(name="Unreferenced", slug="unreferenced")

        result = services.audit_registry()

        self.assertTrue(result["ok"])
        self.assertEqual(result["orphan_projects"], ["unreferenced"])
        self.assertIsNotNone(mcp._tool_manager.get_tool("audit_registry"))

    def test_operating_snapshot_and_infrastructure_are_registered_read_tools(self):
        self.assertIsNotNone(mcp._tool_manager.get_tool("dashboard_snapshot"))
        self.assertIsNotNone(mcp._tool_manager.get_tool("list_managed_resources"))
        self.assertIsNotNone(mcp._tool_manager.get_tool("get_managed_resource"))

    def test_resource_registry_is_discoverable_and_generically_readable(self):
        project = Project.objects.create(name="Generic resource")

        described = services.describe_resources()
        listed = services.list_resource("projects", {"query": "generic"})
        detail = services.get_resource("projects", project.slug)

        self.assertIn("projects", [item["name"] for item in described["resources"]])
        projects = next(item for item in described["resources"] if item["name"] == "projects")
        self.assertEqual(projects["web_route"], "projects:list")
        capabilities = services.describe_capabilities()["capabilities"]
        project_create = next(
            item for item in capabilities if item["name"] == "project.create"
        )
        self.assertEqual(project_create["resource"], "projects")
        self.assertEqual(listed["items"][0]["slug"], project.slug)
        self.assertEqual(detail["slug"], project.slug)
        self.assertIsNotNone(mcp._tool_manager.get_tool("describe_resources"))
        self.assertIsNotNone(mcp._tool_manager.get_tool("list_resource"))
        self.assertIsNotNone(mcp._tool_manager.get_tool("get_resource"))

    def test_connections_are_discoverable_without_credential_material(self):
        described = services.describe_connections()
        listed = services.list_connections()

        self.assertIn(
            "infrastructure.controllers",
            [item["name"] for item in described["connections"]],
        )
        self.assertTrue(listed["ok"])
        self.assertIsNotNone(mcp._tool_manager.get_tool("describe_connections"))
        self.assertIsNotNone(mcp._tool_manager.get_tool("list_connections"))

    def test_derived_topology_is_a_registered_safe_read_tool(self):
        from control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="mcp-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "mcp-cloudflare"},
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=()
        ):
            topology = services.get_topology(
                focus="resource:mcp-zone", direction="inbound", depth=2
            )

        self.assertIn(
            "resource:mcp-zone", {node["id"] for node in topology["nodes"]}
        )
        self.assertEqual(topology["trace"]["focus"], "resource:mcp-zone")
        self.assertEqual(topology["trace"]["direction"], "inbound")
        self.assertIsNotNone(mcp._tool_manager.get_tool("get_topology"))
        self.assertNotIn("secret", json.dumps(topology).lower())

    def test_an_agent_can_ask_the_topology_one_standing_question(self):
        from control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="mcp-lonely", kind="adguard.rewrite",
            spec={"domain": "app.example.test", "answer": "192.0.2.10"})
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            whole = services.get_topology()
            narrowed = services.get_topology(lens="unobserved-resources")
        self.assertEqual(narrowed["lens"], "unobserved-resources")
        self.assertTrue(narrowed["lenses"])
        self.assertIn("resource:mcp-lonely", {n["id"] for n in narrowed["nodes"]})
        self.assertLess(narrowed["summary"]["nodes"], whole["summary"]["nodes"])

    def test_findings_are_a_registered_safe_read_tool(self):
        with mock.patch("application.plugins.plugin_connection_specs", return_value=()):
            found = services.get_findings()
        self.assertTrue(found["ok"])
        self.assertTrue(found["rules"])
        self.assertIsNotNone(mcp._tool_manager.get_tool("get_findings"))
        self.assertNotIn("secret", json.dumps(found).lower())

    def test_connection_state_is_filtered_by_the_mcp_principal(self):
        from application.connections import ConnectionSpec
        from application.security import Capability

        provider = mock.Mock(return_value=())
        spec = ConnectionSpec(
            "example.infrastructure",
            "Infrastructure authority",
            "Requires infrastructure management.",
            Capability.MANAGE_INFRASTRUCTURE,
            provider,
        )
        with mock.patch(
            "application.plugins.plugin_connection_specs", return_value=(spec,)
        ):
            listed = services.list_connections()

        self.assertNotIn(
            "example.infrastructure", [group["name"] for group in listed["groups"]]
        )
        provider.assert_not_called()


class MCPBoundaryTests(TestCase):
    @staticmethod
    async def _allowed_app(scope, receive, send):
        await send(
            {
                "type": "http.response.start",
                "status": 204,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    def _request(
        self,
        *,
        client: str = "100.64.0.10",
        host: str = "a-docker-host",
        token: str | None = TOKEN,
        origin: str | None = None,
        forwarded_for: str | None = None,
        configured_token: str = TOKEN,
        allowed_hosts: tuple[str, ...] = ("a-docker-host",),
    ) -> tuple[int, dict]:
        headers = [(b"host", host.encode())]
        if token is not None:
            headers.append((b"authorization", f"Bearer {token}".encode()))
        if origin:
            headers.append((b"origin", origin.encode()))
        if forwarded_for:
            headers.append((b"x-forwarded-for", forwarded_for.encode()))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": (client, 12345),
            "server": ("100.64.0.20", 8000),
        }
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            sent.append(message)

        boundary = MCPBoundary(
            self._allowed_app,
            token=configured_token,
            allowed_hosts=allowed_hosts,
            allowed_networks=("100.64.0.0/10", "fd7a:115c:a1e0::/48"),
        )
        async_to_sync(boundary)(scope, receive, send)
        status = next(message["status"] for message in sent if "status" in message)
        body = b"".join(message.get("body", b"") for message in sent)
        return status, json.loads(body) if body else {}

    def test_allows_direct_authenticated_tailnet_peer(self):
        status, body = self._request()

        self.assertEqual(status, 204)
        self.assertEqual(body, {})

    def test_denies_lan_peer_even_with_valid_token(self):
        status, body = self._request(client="192.0.2.10")

        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "not_found")

    def test_does_not_trust_spoofed_forwarded_address(self):
        status, _ = self._request(
            client="192.0.2.10", forwarded_for="100.64.0.10"
        )

        self.assertEqual(status, 404)

    def test_rejects_invalid_host(self):
        status, body = self._request(host="hq.jseverino.com")

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_host")

    def test_allows_bracketed_tailnet_ipv6_host(self):
        status, _ = self._request(
            client="fd7a:115c:a1e0::10",
            host="[fd7a:115c:a1e0::20]:8000",
            allowed_hosts=("fd7a:115c:a1e0::20",),
        )

        self.assertEqual(status, 204)

    def test_rejects_browser_origin_by_default(self):
        status, body = self._request(origin="https://evil.example")

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "invalid_origin")

    def test_rejects_missing_or_invalid_token(self):
        for token in (None, "wrong"):
            with self.subTest(token=token):
                status, body = self._request(token=token)
                self.assertEqual(status, 401)
                self.assertEqual(body["error"], "unauthorized")

    def test_disables_endpoint_for_weak_token_or_missing_hosts(self):
        for token, hosts in (("short", ("a-docker-host",)), (TOKEN, ())):
            with self.subTest(token=token, hosts=hosts):
                status, body = self._request(
                    configured_token=token, allowed_hosts=hosts
                )
                self.assertEqual(status, 404)
                self.assertEqual(body["error"], "not_found")
