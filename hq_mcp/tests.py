from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from application import projection
from application.security import Capability, Principal, is_interactive
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
        with self.assertRaisesRegex(services.NotFoundError, "No 'assets' record"):
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
        verifier=None,
        app=None,
        gate=None,
        on_denied=None,
        observer=None,
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
            app or self._allowed_app,
            token=configured_token,
            allowed_hosts=allowed_hosts,
            allowed_networks=("100.64.0.0/10", "fd7a:115c:a1e0::/48"),
            verifier=verifier,
            gate=gate,
            on_denied=on_denied,
            observer=observer,
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


class AgentIdentityTests(MCPBoundaryTests):
    """A Pocket ID token names the agent that presented it.

    The legacy bearer names nobody, so every call it made landed in the audit
    log as one constant actor. These pin the difference, and pin the property
    the approval gate depends on.
    """

    A_JWT = "header.payload.signature"

    def _capturing_app(self):
        """An inner app that records who the boundary said was calling."""

        seen: dict = {}

        async def app(scope, receive, send):
            from .identity import current_principal

            seen["principal"] = current_principal()
            await self._allowed_app(scope, receive, send)

        return app, seen

    @staticmethod
    def _verifier_returning(principal):
        return lambda bearer: principal

    def test_a_verified_token_becomes_the_agent_that_presented_it(self):
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        app, seen = self._capturing_app()

        status, _ = self._request(
            token=self.A_JWT, verifier=self._verifier_returning(agent), app=app
        )

        self.assertEqual(status, 204)
        self.assertEqual(seen["principal"].actor, "example-agent")

    def test_an_agent_is_never_treated_as_a_person(self):
        """The one line the approval gate hangs on.

        `application.approvals` waives the hold for an interactive principal.
        If an agent's interface ever joined `INTERACTIVE_INTERFACES`, every
        infrastructure change it made would self-approve, silently and without
        anything failing.
        """

        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        app, seen = self._capturing_app()

        self._request(
            token=self.A_JWT, verifier=self._verifier_returning(agent), app=app
        )

        self.assertFalse(is_interactive(seen["principal"]))

    def test_capabilities_are_the_token_grant_and_are_never_widened(self):
        granted = frozenset({Capability.READ, Capability.WRITE_PROJECTS})
        agent = Principal("example-agent", "mcp", granted)
        app, seen = self._capturing_app()

        self._request(
            token=self.A_JWT, verifier=self._verifier_returning(agent), app=app
        )

        self.assertEqual(seen["principal"].capabilities, granted)
        self.assertFalse(seen["principal"].permits(Capability.DELETE_PROJECTS))

    def test_the_shared_bearer_still_names_the_service_account(self):
        app, seen = self._capturing_app()

        status, _ = self._request(
            verifier=self._verifier_returning(object()), app=app
        )

        self.assertEqual(status, 204)
        self.assertEqual(seen["principal"].actor, "mcp-service-account")

    def test_a_rejected_token_is_not_then_compared_to_the_shared_secret(self):
        """A failed verification ends the request rather than falling through.

        Otherwise a rejected token would still be measured against the shared
        secret, turning the endpoint into an oracle for it.
        """

        def refuse(bearer):
            raise ValueError("not accepted")

        status, body = self._request(token=self.A_JWT, verifier=refuse)

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_a_verifier_alone_is_enough_to_serve(self):
        """A deployment can retire the shared secret entirely."""

        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        status, _ = self._request(
            token=self.A_JWT,
            configured_token="",
            verifier=self._verifier_returning(agent),
        )

        self.assertEqual(status, 204)

    def test_the_caller_does_not_outlive_the_request(self):
        from .identity import current_principal

        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        self._request(
            token=self.A_JWT, verifier=self._verifier_returning(agent)
        )

        self.assertEqual(current_principal().actor, "mcp-service-account")


class AgentBrakeTests(MCPBoundaryTests):
    A_JWT = "header.payload.signature"

    @staticmethod
    def _gate(allowed):
        async def gate():
            return allowed

        return gate

    def _agent(self):
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))
        return lambda bearer: agent

    def test_a_paused_agent_is_refused_before_anything_is_dispatched(self):
        reached = []

        async def app(scope, receive, send):
            reached.append(True)
            await self._allowed_app(scope, receive, send)

        status, body = self._request(
            token=self.A_JWT, verifier=self._agent(), gate=self._gate(False), app=app
        )

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "agents_paused")
        self.assertEqual(reached, [], "a paused agent must not reach a tool, or the tool list")

    def test_the_shared_bearer_is_paused_too(self):
        status, body = self._request(gate=self._gate(False))

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "agents_paused")

    def test_an_unreadable_switch_refuses(self):
        async def broken():
            raise RuntimeError("database locked")

        status, body = self._request(token=self.A_JWT, verifier=self._agent(), gate=broken)

        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "agents_paused")

    def test_only_an_authenticated_caller_learns_agents_are_paused(self):
        status, body = self._request(token="wrong", gate=self._gate(False))

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_allowed_agents_pass(self):
        status, _ = self._request(token=self.A_JWT, verifier=self._agent(), gate=self._gate(True))

        self.assertEqual(status, 204)


class GrantCeilingTests(TestCase):
    def _claims(self, *scopes):
        return {"client_id": "example-agent", "scope": " ".join(scopes)}

    def test_a_grant_the_deployment_withholds_is_not_held(self):
        from django.test import override_settings

        from .identity import token_principal

        with override_settings(SEVERINO_MCP_ENABLE_WRITES=False):
            agent = token_principal(self._claims("read", "write_projects"))

        self.assertTrue(agent.permits(Capability.READ))
        self.assertFalse(agent.permits(Capability.WRITE_PROJECTS))

    def test_a_grant_the_deployment_allows_is_held(self):
        from django.test import override_settings

        from .identity import token_principal

        with override_settings(SEVERINO_MCP_ENABLE_WRITES=True):
            agent = token_principal(self._claims("read", "write_projects"))

        self.assertTrue(agent.permits(Capability.WRITE_PROJECTS))

    def test_the_ceiling_never_widens_a_grant(self):
        from django.test import override_settings

        from .identity import token_principal

        with override_settings(SEVERINO_MCP_ENABLE_WRITES=True):
            agent = token_principal(self._claims("read"))

        self.assertFalse(agent.permits(Capability.WRITE_PROJECTS))


class DoorRefusalTests(MCPBoundaryTests):
    A_JWT = "header.payload.signature"

    def _recorder(self):
        seen = []

        async def record(**fields):
            seen.append(fields)

        return record, seen

    def test_a_bad_credential_is_counted_as_unauthenticated_with_its_source(self):
        record, seen = self._recorder()

        self._request(token="wrong", on_denied=record)

        self.assertEqual(seen, [{"reason": "invalid_credential", "source": "100.64.0.10", "authenticated": False}])

    def test_a_missing_credential_is_counted(self):
        record, seen = self._recorder()

        self._request(token=None, on_denied=record)

        self.assertEqual(seen[0]["reason"], "missing_credential")

    def test_a_paused_agent_is_recorded_by_name(self):
        record, seen = self._recorder()
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        async def paused():
            return False

        self._request(token=self.A_JWT, verifier=lambda bearer: agent, gate=paused, on_denied=record)

        self.assertEqual(seen[0]["reason"], "agents_paused")
        self.assertEqual(seen[0]["actor"], "example-agent")

    def test_a_recorder_that_fails_does_not_change_the_answer(self):
        async def broken(**fields):
            raise RuntimeError("audit down")

        status, body = self._request(token="wrong", on_denied=broken)

        self.assertEqual(status, 401)
        self.assertEqual(body["error"], "unauthorized")

    def test_an_admitted_request_records_nothing(self):
        record, seen = self._recorder()

        status, _ = self._request(on_denied=record)

        self.assertEqual(status, 204)
        self.assertEqual(seen, [])


class ObservationTests(MCPBoundaryTests):
    A_JWT = "header.payload.signature"

    def test_an_authenticated_agent_is_observed_even_while_paused(self):
        seen = []
        agent = Principal("example-agent", "mcp", frozenset({Capability.READ}))

        async def observer(principal):
            seen.append(principal.actor)

        async def paused():
            return False

        status, _ = self._request(
            token=self.A_JWT, verifier=lambda bearer: agent, gate=paused, observer=observer
        )

        self.assertEqual(status, 403)
        self.assertEqual(seen, ["example-agent"])

    def test_a_rejected_credential_is_not_observed(self):
        seen = []

        async def observer(principal):
            seen.append(principal)

        self._request(token="wrong", observer=observer)

        self.assertEqual(seen, [])
