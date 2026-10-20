import json
import threading
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, TestCase, override_settings
from jwt.algorithms import RSAAlgorithm

from hq.platform.application.security import AuthorizationError
from hq.platform.application.tests.host_only import host_only
from hq.platform.application.tests.sentinel import (
    CONNECTION as SENTINEL_CONNECTION,
    READING as SENTINEL_READING,
    REWRITE as SENTINEL_REWRITE,
    SentinelMixin,
    plant_credentials,
)

from .. import security, views
from ..testing import ContractClient

ISSUER = "https://sso.example.test"
RESOURCE = "https://hq.example.test/api"
KID = "example-key"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key=_KEY, **overrides) -> str:
    now = datetime.now(tz=UTC)
    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "example-automation",
        "client_id": "example-automation",
        "scope": "example.write",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": KID})


def _held(key):
    jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {KID: jwt.PyJWK({**jwk, "kid": KID, "use": "sig", "alg": "RS256"})}


def _serving(key=_KEY):
    """The identity provider's published key, held as the key thread holds it."""

    return patch.object(security.KEYS, "held", _held(key))


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class TokenVerificationTests(SimpleTestCase):
    def test_a_valid_token_verifies(self):
        with _serving():
            claims = security.verify(_token())
        self.assertEqual(claims["client_id"], "example-automation")

    def test_a_token_signed_by_another_key_is_rejected(self):
        with _serving(), self.assertRaises(security.TokenError):
            security.verify(_token(key=_OTHER_KEY))

    def test_a_token_for_another_api_resource_is_rejected(self):
        """The case signature-checking alone would wave through.

        A token minted for a different API on the same Pocket ID instance is
        signed by the very same key, so `aud` is the only thing standing
        between that credential and this one.
        """

        with _serving(), self.assertRaises(security.TokenError):
            security.verify(_token(aud="https://elsewhere.example.test"))

    def test_a_token_from_another_issuer_is_rejected(self):
        with _serving(), self.assertRaises(security.TokenError):
            security.verify(_token(iss="https://evil.example.test"))

    def test_an_expired_token_is_rejected(self):
        past = datetime.now(tz=UTC) - timedelta(hours=1)
        with _serving(), self.assertRaises(security.TokenError):
            security.verify(_token(exp=past))

    def test_a_token_without_an_expiry_is_rejected(self):
        """A token that never expires is a password, and is refused as one."""

        now = datetime.now(tz=UTC)
        forever = jwt.encode(
            {"iss": ISSUER, "aud": RESOURCE, "sub": "x", "iat": now},
            _KEY,
            algorithm="RS256",
        )
        with _serving(), self.assertRaises(security.TokenError):
            security.verify(forever)

    @override_settings(OIDC_OP_JWKS_ENDPOINT=f"{ISSUER}/.well-known/jwks.json")
    def test_the_key_set_is_read_on_a_thread_of_its_own_when_the_application_starts(self):
        reader = []

        def get_signing_keys(client):
            reader.append(threading.current_thread())
            return list(_held(_KEY).values())

        with (
            patch.object(security.KEYS, "held", {}),
            patch.object(security.PyJWKClient, "get_signing_keys", get_signing_keys),
        ):
            with security.keeping_keys():
                pass
            self.assertEqual(security.verify(_token())["client_id"], "example-automation")
        self.assertEqual(len(reader), 1)
        self.assertIsNot(reader[0], threading.current_thread())

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_an_unconfigured_resource_fails_closed(self):
        self.assertFalse(security.is_configured())
        with _serving(), self.assertRaises(security.TokenError):
            security.verify(_token())


class GrantTests(SimpleTestCase):
    def test_space_delimited_scope_is_read(self):
        self.assertEqual(
            security.granted({"scope": "example.write read"}),
            frozenset({"example.write", "read"}),
        )

    def test_a_list_claim_is_read(self):
        self.assertEqual(
            security.granted({"permissions": ["example.write"]}),
            frozenset({"example.write"}),
        )

    def test_no_grant_claim_yields_nothing(self):
        self.assertEqual(security.granted({"sub": "x"}), frozenset())

    def test_a_principal_holds_exactly_what_the_token_granted(self):
        """The narrow grant is the entire point of this surface.

        A web operator holds every capability HQ has. If a verified client were
        given the same set, a credential sitting on a phone could delete a
        project, and the OAuth ceremony would have bought nothing.
        """

        principal = security.api_principal({"scope": "example.write", "sub": "s"})
        self.assertEqual(principal.capabilities, frozenset({"example.write"}))
        self.assertEqual(principal.interface, "api")
        principal.require("example.write")
        with self.assertRaises(AuthorizationError):
            principal.require("delete_projects")

    def test_a_token_granting_nothing_is_refused_outright(self):
        with self.assertRaises(AuthorizationError):
            security.api_principal({"sub": "s"})

    def test_the_actor_is_the_client_that_presented_the_token(self):
        principal = security.api_principal({"scope": "read", "client_id": "example-automation", "sub": "s"})
        self.assertEqual(principal.actor, "example-automation")


class CompositionCheckTests(SimpleTestCase):
    def test_compiler_violations_are_reported_together(self):
        from hq.platform.application.integrations import (
            IntegrationGraphError,
            IntegrationViolation,
        )

        from ..checks import capability_contract_check

        failure = IntegrationGraphError(
            (
                IntegrationViolation("example.first", "First violation."),
                IntegrationViolation("example.second", "Second violation."),
            )
        )
        with patch("hq.platform.api.checks.integration_graph", side_effect=failure):
            errors = capability_contract_check(None)

        self.assertEqual([error.id for error in errors], ["hq_api.E001"] * 2)
        self.assertEqual(
            [error.hint for error in errors],
            [
                "Integration violation: example.first",
                "Integration violation: example.second",
            ],
        )

    def test_an_unresolvable_resource_route_is_a_named_startup_error(self):
        from hq.platform.application.integrations import (
            compile_integration_graph,
            override_integration_graph,
        )
        from hq.platform.application.resources import EmptyQuery, ResourceSpec

        from ..checks import capability_contract_check

        resource = ResourceSpec(
            "example.records",
            "Records",
            "Synthetic records.",
            "read",
            list_handler=lambda: {"items": [], "count": 0},
            list_query_type=EmptyQuery,
            web_route="missing:list",
        )
        graph = compile_integration_graph(capabilities=(), resources=(resource,), connections=())
        with override_integration_graph(graph):
            errors = capability_contract_check(None)

        self.assertEqual([error.id for error in errors], ["hq_api.E004"])
        self.assertIn("missing:list", errors[0].msg)

    def test_an_unresolvable_connection_route_is_a_named_startup_error(self):
        from hq.platform.application.connections import ConnectionSpec
        from hq.platform.application.integrations import (
            compile_integration_graph,
            override_integration_graph,
        )

        from ..checks import capability_contract_check

        connection = ConnectionSpec(
            "example.finance",
            "Finance",
            "Financial institutions.",
            "read",
            lambda: (),
            web_route="missing:connections",
        )
        graph = compile_integration_graph(capabilities=(), resources=(), connections=(connection,))
        with override_integration_graph(graph):
            errors = capability_contract_check(None)

        self.assertEqual([error.id for error in errors], ["hq_api.E006"])


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class TransportTests(SentinelMixin, TestCase):
    # Every response below is also held to the OpenAPI document.
    client_class = ContractClient

    def _post(self, name, body, token=None, idempotency_key="test-key"):
        headers = {}
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        if idempotency_key is not None:
            headers["HTTP_IDEMPOTENCY_KEY"] = idempotency_key
        return self.client.post(
            f"/api/v2/capabilities/{name}/",
            data=json.dumps(body),
            content_type="application/json",
            **headers,
        )

    def test_a_missing_token_is_401_and_never_a_login_redirect(self):
        """The failure a Shortcut has to be able to act on.

        Every other HQ URL answers an anonymous request with a 302 to an HTML
        login page. A client that cannot fill one in would record that redirect
        as success and silently import nothing.
        """

        response = self.client.get("/api/v2/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], views.REALM)
        self.assertEqual(response.json()["ok"], False)

    def test_a_rejected_token_is_401(self):
        with _serving():
            response = self._post("example.import", {}, token=_token(key=_OTHER_KEY))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_token")

    @override_settings(OIDC_OP_JWKS_ENDPOINT=f"{ISSUER}/.well-known/jwks.json")
    def test_a_token_is_never_verified_by_reaching_the_identity_provider(self):
        """Keys not yet read, or a key not held, answer 401 and ask for a read."""

        security.KEYS.wanted.clear()
        for case, held in (("not loaded", {}), ("unknown key", {"another-key": _held(_KEY)[KID]})):
            with (
                self.subTest(case),
                patch.object(security.KEYS, "held", held),
                self.assertLogs("severino.api", "WARNING") as logged,
            ):
                response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["error"]["code"], "invalid_token")
            self.assertTrue(security.KEYS.wanted.is_set())
            self.assertIn(logged.records[0].event, {"api.keys.not_loaded", "api.keys.unknown"})
            security.KEYS.wanted.clear()

    def test_an_ungranted_capability_is_403(self):
        with _serving():
            response = self._post("project.create", {"payload": {}}, token=_token(scope="example.write"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_a_paused_agent_is_refused_on_the_machine_api(self):
        """The same brake /mcp/ applies, on the other door.

        The agents hold these same tokens, so pausing them on one surface
        alone would leave every capability one URL away.
        """

        from hq.domains.projects.models import Project
        from hq.platform.core.models import AgentAccess

        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})
        body = {"payload": {"name": "Paused", "slug": "paused", "status": "active"}}
        with _serving():
            write = self._post("project.create", body, token=_token(scope="write_projects"))
            read = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}")
        for response in (write, read):
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["error"]["code"], "agents_paused")
        self.assertFalse(Project.objects.filter(slug="paused").exists())

    def test_only_an_authenticated_caller_learns_agents_are_paused(self):
        from hq.platform.core.models import AgentAccess

        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": True})
        with _serving():
            response = self._post("project.create", {}, token=_token(key=_OTHER_KEY))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_token")

    def test_an_unreadable_switch_refuses_on_the_machine_api(self):
        from django.db import DatabaseError

        with (
            _serving(),
            patch(
                "hq.platform.application.agent_access.AgentAccess.objects.filter",
                side_effect=DatabaseError("locked"),
            ),
        ):
            response = self._post(
                "project.create",
                {"payload": {"name": "X", "slug": "x", "status": "active"}},
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "agents_paused")

    def test_a_resumed_agent_runs_again(self):
        """The positive control: without it a brake that refused everything would pass."""

        from hq.domains.projects.models import Project
        from hq.platform.core.models import AgentAccess

        AgentAccess.objects.update_or_create(pk=1, defaults={"paused": False})
        with _serving():
            response = self._post(
                "project.create",
                {"payload": {"name": "Resumed", "slug": "resumed", "status": "active"}},
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(Project.objects.filter(slug="resumed").exists())

    def test_a_granted_capability_actually_runs(self):
        """The half a deny-only test cannot prove.

        A permission key on the Pocket ID resource and a capability name in
        HQ's registry are the same string by design; this is what fails if
        anything ever starts translating between them.
        """

        from hq.domains.projects.models import Project

        with _serving():
            response = self._post(
                "project.create",
                {
                    "payload": {
                        "name": "Machine sync",
                        "slug": "machine-sync",
                        "status": "active",
                    }
                },
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(Project.objects.filter(slug="machine-sync").exists())

    def test_a_machine_write_requires_an_idempotency_key(self):
        with _serving():
            response = self._post(
                "project.create",
                {
                    "payload": {
                        "name": "Once",
                        "slug": "once",
                        "status": "active",
                    }
                },
                token=_token(scope="write_projects"),
                idempotency_key=None,
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")

    def test_retrying_a_machine_write_replays_the_committed_response(self):
        from hq.domains.projects.models import Project

        body = {"payload": {"name": "Once", "slug": "once", "status": "active"}}
        with _serving():
            first = self._post(
                "project.create",
                body,
                token=_token(scope="write_projects"),
                idempotency_key="one-operation",
            )
            replay = self._post(
                "project.create",
                body,
                token=_token(scope="write_projects"),
                idempotency_key="one-operation",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json(), first.json())
        self.assertEqual(replay["Idempotency-Replayed"], "true")
        self.assertEqual(Project.objects.filter(slug="once").count(), 1)

    def test_an_idempotency_key_cannot_be_reused_for_another_request(self):
        with _serving():
            first = self._post(
                "project.create",
                {
                    "payload": {
                        "name": "First",
                        "slug": "first",
                        "status": "active",
                    }
                },
                token=_token(scope="write_projects"),
                idempotency_key="same-key",
            )
            conflict = self._post(
                "project.create",
                {
                    "payload": {
                        "name": "Second",
                        "slug": "second",
                        "status": "active",
                    }
                },
                token=_token(scope="write_projects"),
                idempotency_key="same-key",
            )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "idempotency_conflict")

    def test_the_audit_trail_names_the_client_not_the_operator(self):
        """Revocability is worth little if you cannot tell what a token did."""

        from hq.platform.core.models import AuditLog

        with _serving():
            self._post(
                "project.create",
                {
                    "payload": {
                        "name": "Audited",
                        "slug": "audited",
                        "status": "active",
                    }
                },
                token=_token(scope="write_projects"),
            )
        self.assertTrue(AuditLog.objects.filter(object_repr__icontains="Audited").exists())

    def test_an_unknown_capability_is_404(self):
        with _serving():
            response = self._post("nope.nothing", {"payload": {}}, token=_token())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_capability")

    def test_a_malformed_body_is_reported_as_such(self):
        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/example.import/",
                data="not json",
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token()}",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_unknown_envelope_fields_are_rejected_instead_of_ignored(self):
        with _serving():
            response = self._post(
                "project.create",
                {"paylod": {}},
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_an_unknown_payload_field_is_named_in_a_400(self):
        with _serving():
            response = self._post(
                "project.create",
                {"payload": {"name": "Typo", "slug": "typo", "stauts": "active"}},
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_input")
        self.assertIn("stauts", error["message"])

    def test_an_invalid_payload_names_the_field_in_a_400(self):
        with _serving():
            response = self._post(
                "project.create",
                {"payload": {"slug": "nameless"}},
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 400)
        error = response.json()["error"]
        self.assertEqual(error["code"], "invalid_input")
        self.assertEqual(error["message"], "project.create: name is required.")
        self.assertEqual(error["details"][0]["type"], "missing")

    def test_a_refused_value_appears_nowhere_in_the_response(self):
        """Neither message nor details repeat what was sent: it may be a secret."""

        secret = "hunter2-not-for-logs"
        token = _token(scope="write_projects")
        payloads = {
            "schema": {"name": [secret], "slug": "schema"},
            "domain": {"name": "Domain", "slug": "domain", "status": secret},
        }
        for case, payload in payloads.items():
            with self.subTest(case), _serving():
                response = self._post(
                    "project.create",
                    {"payload": payload},
                    token=token,
                    idempotency_key=f"secret-{case}",
                )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["error"]["code"], "invalid_input")
            self.assertNotIn(secret, response.content.decode())

        with _serving():
            listed = self.client.get(
                f"/api/v2/resources/projects/?limit={secret}",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
            )
        self.assertEqual(listed.status_code, 400)
        self.assertEqual(
            listed.json()["error"]["message"],
            "projects: limit must be a valid integer.",
        )
        self.assertNotIn(secret, listed.content.decode())

    def test_payload_cannot_be_a_falsey_non_object(self):
        with _serving():
            response = self._post(
                "project.create",
                {"payload": []},
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_input")

    def test_duplicate_json_fields_are_rejected(self):
        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/project.create/",
                data='{"payload":{},"payload":{}}',
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                HTTP_IDEMPOTENCY_KEY="test-key",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_execute_requires_json_content(self):
        with _serving():
            response = self.client.post(
                "/api/v2/capabilities/project.create/",
                data="payload=not-json",
                content_type="application/x-www-form-urlencoded",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
                HTTP_IDEMPOTENCY_KEY="test-key",
            )
        self.assertEqual(response.status_code, 415)

    def test_get_is_refused_on_an_execute_route(self):
        with _serving():
            response = self.client.get(
                "/api/v2/capabilities/example.import/",
                HTTP_AUTHORIZATION=f"Bearer {_token()}",
            )
        self.assertEqual(response.status_code, 405)

    def test_the_root_reports_what_the_credential_may_do(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["granted"], ["example.write"])
        self.assertEqual(data["actor"], "example-automation")
        self.assertEqual(data["resource"], RESOURCE)
        self.assertEqual(data["api_version"], 2)
        self.assertEqual(data["links"]["resources"], "/api/v2/resources/")
        self.assertEqual(data["links"]["connections"], "/api/v2/connections/")
        self.assertEqual(data["links"]["topology"], "/api/v2/topology/")

    def test_capabilities_flag_what_this_token_may_run(self):
        with _serving():
            response = self.client.get("/api/v2/capabilities/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response.status_code, 200)
        specs = response.json()["data"]["capabilities"]
        self.assertTrue(specs)
        # Nothing in HQ core is grantable by `example.write` alone, so a token
        # scoped to it must not come back permitted for anything here.
        self.assertFalse([spec for spec in specs if spec["permitted"]])
        project_create = next(spec for spec in specs if spec["name"] == "project.create")
        self.assertTrue(project_create["idempotency_key_required"])
        self.assertFalse(project_create["request_schema"]["additionalProperties"])
        self.assertEqual(project_create["resource"], "projects")
        self.assertEqual(
            project_create["request_schema"]["properties"]["payload"],
            project_create["input_schema"],
        )
        project_update = next(spec for spec in specs if spec["name"] == "project.update")
        self.assertEqual(project_update["request_schema"]["required"], ["target"])
        nested = views._request_schema(
            {
                "input_schema": {
                    "type": "object",
                    "$defs": {"Record": {"type": "object"}},
                    "properties": {"record": {"$ref": "#/$defs/Record"}},
                },
                "target": None,
            }
        )
        self.assertIn("$defs", nested)
        self.assertNotIn("$defs", nested["properties"]["payload"])

    def test_resources_are_discoverable_and_flagged_for_this_token(self):
        with _serving():
            response = self.client.get(
                "/api/v2/resources/",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
            )
        self.assertEqual(response.status_code, 200)
        specs = response.json()["data"]["resources"]
        projects = next(spec for spec in specs if spec["name"] == "projects")
        audit = next(spec for spec in specs if spec["name"] == "audit")
        self.assertTrue(projects["permitted"])
        self.assertFalse(audit["permitted"])
        self.assertIn("query_schema", projects["operations"]["list"])
        self.assertEqual(projects["web_route"], "projects:list")

    def test_connections_expose_abilities_and_safe_cached_state(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ProviderConnection

        ProviderConnection.objects.create(
            connection_ref="api-cloudflare",
            controller_id="controller",
            provider="cloudflare_dns",
            reaches=["example.com"],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )
        with _serving():
            response = self.client.get(
                "/api/v2/connections/",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        core = next(item for item in data["connections"] if item["name"] == "infrastructure.controllers")
        state = next(
            item for group in data["groups"] for item in group["instances"] if item["id"] == "controller:api-cloudflare"
        )
        self.assertTrue(core["permitted"])
        self.assertIn("cloudflare.dns_record", {ability["name"] for ability in state["abilities"]})

    def test_connections_never_invoke_a_family_the_token_cannot_read(self):
        with (
            _serving(),
            patch("hq.platform.application.connections._controller_instances") as provider,
        ):
            response = self.client.get(
                "/api/v2/connections/",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        core = next(item for item in data["connections"] if item["name"] == "infrastructure.controllers")
        self.assertFalse(core["permitted"])
        self.assertEqual(data["groups"], [])
        provider.assert_not_called()

    def test_topology_exposes_safe_nodes_edges_and_canonical_actions(self):
        from django.utils import timezone

        from hq.domains.control_plane.models import ManagedResource, ProviderConnection

        ManagedResource.objects.create(
            key="api-zone",
            kind="cloudflare.zone",
            spec={"zone": "example.com", "connection_ref": "api-cloudflare"},
        )
        ProviderConnection.objects.create(
            connection_ref="api-cloudflare",
            controller_id="api-controller",
            provider="cloudflare_dns",
            reaches=["example.com"],
            reachable=True,
            probed=True,
            observed_at=timezone.now(),
        )
        with _serving(), host_only():
            response = self.client.get(
                "/api/v2/topology/?focus=resource%3Aapi-zone&direction=inbound&depth=2",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
            )

        self.assertEqual(response.status_code, 200)
        topology = response.json()["data"]
        self.assertEqual(topology["schema_version"], 2)
        self.assertEqual(topology["trace"]["focus"], "resource:api-zone")
        self.assertEqual(topology["trace"]["direction"], "inbound")
        self.assertEqual(topology["trace"]["depth"], 2)
        self.assertEqual(topology["trace"]["hops"][0], {"node": "resource:api-zone", "hop": 0})
        self.assertIn("resource:api-zone", {node["id"] for node in topology["nodes"]})
        self.assertTrue(topology["edges"])
        resource = next(node for node in topology["nodes"] if node["id"] == "resource:api-zone")
        self.assertEqual([action["name"] for action in resource["actions"]], ["open"])

    def test_unknown_topology_focus_is_explicitly_not_applied(self):
        with _serving(), host_only():
            response = self.client.get(
                "/api/v2/topology/?focus=not-a-node&direction=outbound&depth=999",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='read')}",
            )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["data"]["trace"])

    def test_topology_requires_read_before_deriving_any_state(self):
        with (
            _serving(),
            patch("hq.platform.application.topology.connection_catalog") as catalog,
        ):
            response = self.client.get(
                "/api/v2/topology/",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='write_projects')}",
            )

        self.assertEqual(response.status_code, 403)
        catalog.assert_not_called()

    def test_no_read_serves_a_credential_a_controller_reported(self):
        plant_credentials()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            listed = self.client.get("/api/v2/resources/", **headers).json()["data"]["resources"]
            reads = ["/api/v2/connections/", "/api/v2/topology/", "/api/v2/findings/"] + [
                f"/api/v2/resources/{spec['name']}/"
                for spec in listed
                if spec["permitted"] and spec["operations"].get("list")
            ]
            reads += [
                f"/api/v2/resources/readings/{SENTINEL_READING}/",
                "/api/v2/resources/infrastructure.resources/sentinel-rewrite/",
            ]
            served = ""
            for url in reads:
                with self.subTest(url):
                    response = self.client.get(url, **headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertNoSentinel(response.content)
                    served += response.content.decode()

        # The planted connection and reading were served: the absence is not vacuous.
        self.assertIn(SENTINEL_CONNECTION, served)
        self.assertIn(SENTINEL_REWRITE, served)

    def test_resource_list_and_detail_share_the_declared_contract(self):
        from hq.domains.projects.models import Project

        project = Project.objects.create(name="Machine readable")
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            listed = self.client.get("/api/v2/resources/projects/?query=readable", **headers)
            detail = self.client.get(f"/api/v2/resources/projects/{project.slug}/", **headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"]["items"][0]["slug"], project.slug)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["data"]["slug"], project.slug)

    def test_hq_answers_how_it_is_over_the_api(self):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            response = self.client.get("/api/v2/resources/hq/", **headers)
        self.assertEqual(response.status_code, 200)
        found = response.json()["data"]
        self.assertEqual(found["count"], len(found["items"]))
        self.assertIn(found["status"], ("good", "attention", "serious"))
        self.assertIn("release", found)
        # A request reached it, so its path is said.
        self.assertIsNotNone(found["reached"])

    def test_resource_queries_reject_unknown_and_repeated_fields(self):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            unknown = self.client.get("/api/v2/resources/projects/?limti=10", **headers)
            repeated = self.client.get("/api/v2/resources/projects/?limit=1&limit=2", **headers)
        self.assertEqual(unknown.status_code, 400)
        self.assertEqual(unknown.json()["error"]["code"], "invalid_input")
        self.assertEqual(unknown.json()["error"]["message"], "projects: limti is not a known field.")
        self.assertEqual(repeated.status_code, 400)

    def test_a_resource_read_keeps_only_the_fields_asked_for(self):
        from hq.domains.projects.models import Project

        project = Project.objects.create(name="Selected over the wire")
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            detail = self.client.get(f"/api/v2/resources/projects/{project.slug}/?fields=slug,name", **headers)
            listed = self.client.get("/api/v2/resources/projects/?query=wire&fields=slug", **headers)
            unknown = self.client.get(f"/api/v2/resources/projects/{project.slug}/?fields=slgu", **headers)
            repeated = self.client.get(f"/api/v2/resources/projects/{project.slug}/?fields=a&fields=b", **headers)
            into_scalar = self.client.get(f"/api/v2/resources/projects/{project.slug}/?fields=slug.x", **headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(set(detail.json()["data"]), {"slug", "name"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["data"]["items"], [{"slug": project.slug}])
        self.assertEqual(listed.json()["data"]["count"], 1)
        for refused in (unknown, repeated, into_scalar):
            self.assertEqual(refused.status_code, 400)
            self.assertEqual(refused.json()["error"]["code"], "invalid_input")

    def test_a_field_path_reaches_into_each_record_a_reading_holds(self):
        plant_credentials()
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving():
            response = self.client.get(
                f"/api/v2/resources/readings/{SENTINEL_READING}/?fields=observed_at,items.record", **headers
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(set(data), {"observed_at", "items"})
        self.assertTrue(data["items"])
        self.assertTrue(all(set(item) == {"record"} for item in data["items"]))

    def test_findings_narrow_by_floor_and_limit_and_refuse_what_cannot(self):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {_token(scope='read')}"}
        with _serving(), host_only():
            narrowed = self.client.get("/api/v2/findings/?severity=attention&limit=1", **headers)
            refused = {
                value: self.client.get(f"/api/v2/findings/?{value}", **headers)
                for value in ("limit=many", "limit=0", "severity=critical")
            }
        self.assertEqual(narrowed.status_code, 200)
        self.assertLessEqual(len(narrowed.json()["data"]["findings"]), 1)
        self.assertIn("truncated", narrowed.json()["data"]["summary"])
        for value, response in refused.items():
            with self.subTest(value):
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["error"]["code"], "invalid_input")
                self.assertNotIn(value.split("=")[1], response.json()["error"]["message"])

    def test_resource_read_requires_its_declared_grant(self):
        with _serving():
            response = self.client.get(
                "/api/v2/resources/projects/",
                HTTP_AUTHORIZATION=f"Bearer {_token(scope='example.write')}",
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_a_response_is_never_cacheable(self):
        with _serving():
            response = self.client.get("/api/v2/", HTTP_AUTHORIZATION=f"Bearer {_token()}")
        self.assertEqual(response["Cache-Control"], "private, no-store")

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_the_surface_is_off_until_a_resource_is_configured(self):
        response = self.client.get("/api/v2/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "not_configured")
