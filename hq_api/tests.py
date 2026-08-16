from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone as datetime_timezone
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.test import SimpleTestCase, TestCase, override_settings

from application.security import AuthorizationError

from . import security, views

ISSUER = "https://sso.example.test"
RESOURCE = "https://hq.example.test/api"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(key=_KEY, **overrides) -> str:
    now = datetime.now(tz=datetime_timezone.utc)
    claims = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "health-sync-shortcut",
        "client_id": "health-sync-shortcut",
        "scope": "fitness.write",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256")


class _FakeJWKS:
    """Stands in for the identity provider's published key set."""

    def __init__(self, key):
        self._key = key

    def get_signing_key_from_jwt(self, token):  # noqa: ARG002 - signature parity
        return type("Key", (), {"key": self._key.public_key()})()


def _serving(key=_KEY):
    return patch.object(security, "_jwks", lambda: _FakeJWKS(key))


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
        self.assertEqual(claims["client_id"], "health-sync-shortcut")

    def test_a_token_signed_by_another_key_is_rejected(self):
        with _serving():
            with self.assertRaises(security.TokenError):
                security.verify(_token(key=_OTHER_KEY))

    def test_a_token_for_another_api_resource_is_rejected(self):
        """The case signature-checking alone would wave through.

        A token minted for a different API on the same Pocket ID instance is
        signed by the very same key, so `aud` is the only thing standing
        between that credential and this one.
        """

        with _serving():
            with self.assertRaises(security.TokenError):
                security.verify(_token(aud="https://elsewhere.example.test"))

    def test_a_token_from_another_issuer_is_rejected(self):
        with _serving():
            with self.assertRaises(security.TokenError):
                security.verify(_token(iss="https://evil.example.test"))

    def test_an_expired_token_is_rejected(self):
        past = datetime.now(tz=datetime_timezone.utc) - timedelta(hours=1)
        with _serving():
            with self.assertRaises(security.TokenError):
                security.verify(_token(exp=past))

    def test_a_token_without_an_expiry_is_rejected(self):
        """A token that never expires is a password, and is refused as one."""

        now = datetime.now(tz=datetime_timezone.utc)
        forever = jwt.encode(
            {"iss": ISSUER, "aud": RESOURCE, "sub": "x", "iat": now},
            _KEY,
            algorithm="RS256",
        )
        with _serving():
            with self.assertRaises(security.TokenError):
                security.verify(forever)

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_an_unconfigured_resource_fails_closed(self):
        self.assertFalse(security.is_configured())
        with _serving():
            with self.assertRaises(security.TokenError):
                security.verify(_token())


class GrantTests(SimpleTestCase):
    def test_space_delimited_scope_is_read(self):
        self.assertEqual(
            security.granted({"scope": "fitness.write read"}),
            frozenset({"fitness.write", "read"}),
        )

    def test_a_list_claim_is_read(self):
        self.assertEqual(
            security.granted({"permissions": ["fitness.write"]}),
            frozenset({"fitness.write"}),
        )

    def test_no_grant_claim_yields_nothing(self):
        self.assertEqual(security.granted({"sub": "x"}), frozenset())

    def test_a_principal_holds_exactly_what_the_token_granted(self):
        """The narrow grant is the entire point of this surface.

        A web operator holds every capability HQ has. If a verified client were
        given the same set, a credential sitting on a phone could delete a
        project, and the OAuth ceremony would have bought nothing.
        """

        principal = security.api_principal({"scope": "fitness.write", "sub": "s"})
        self.assertEqual(principal.capabilities, frozenset({"fitness.write"}))
        self.assertEqual(principal.interface, "api")
        principal.require("fitness.write")
        with self.assertRaises(AuthorizationError):
            principal.require("delete_projects")

    def test_a_token_granting_nothing_is_refused_outright(self):
        with self.assertRaises(AuthorizationError):
            security.api_principal({"sub": "s"})

    def test_the_actor_is_the_client_that_presented_the_token(self):
        principal = security.api_principal(
            {"scope": "read", "client_id": "health-sync-shortcut", "sub": "s"}
        )
        self.assertEqual(principal.actor, "health-sync-shortcut")


@override_settings(
    OIDC_ISSUER=ISSUER,
    SEVERINO_API_RESOURCE=RESOURCE,
    SEVERINO_API_LEEWAY_SECONDS=30,
    OIDC_RP_SIGN_ALGO="RS256",
)
class TransportTests(TestCase):
    def _post(self, name, body, token=None):
        headers = {}
        if token is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(
            f"/api/v1/capabilities/{name}/",
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

        response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response["WWW-Authenticate"], views.REALM)
        self.assertEqual(response.json()["ok"], False)

    def test_a_rejected_token_is_401(self):
        with _serving():
            response = self._post("fitness.import", {}, token=_token(key=_OTHER_KEY))
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "invalid_token")

    def test_an_ungranted_capability_is_403(self):
        with _serving():
            response = self._post(
                "project.create", {"payload": {}}, token=_token(scope="fitness.write")
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "forbidden")

    def test_a_granted_capability_actually_runs(self):
        """The half a deny-only test cannot prove.

        A permission key on the Pocket ID resource and a capability name in
        HQ's registry are the same string by design; this is what fails if
        anything ever starts translating between them.
        """

        from projects.models import Project

        with _serving():
            response = self._post(
                "project.create",
                {
                    "payload": {
                        "name": "Health sync",
                        "slug": "health-sync",
                        "status": "active",
                    }
                },
                token=_token(scope="write_projects"),
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()["ok"])
        self.assertTrue(Project.objects.filter(slug="health-sync").exists())

    def test_the_audit_trail_names_the_client_not_the_operator(self):
        """Revocability is worth little if you cannot tell what a token did."""

        from core.models import AuditLog

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
        self.assertTrue(
            AuditLog.objects.filter(object_repr__icontains="Audited").exists()
        )

    def test_an_unknown_capability_is_404(self):
        with _serving():
            response = self._post("nope.nothing", {"payload": {}}, token=_token())
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "unknown_capability")

    def test_a_malformed_body_is_reported_as_such(self):
        with _serving():
            response = self.client.post(
                "/api/v1/capabilities/fitness.import/",
                data="not json",
                content_type="application/json",
                HTTP_AUTHORIZATION=f"Bearer {_token()}",
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_json")

    def test_get_is_refused_on_an_execute_route(self):
        with _serving():
            response = self.client.get(
                "/api/v1/capabilities/fitness.import/",
                HTTP_AUTHORIZATION=f"Bearer {_token()}",
            )
        self.assertEqual(response.status_code, 405)

    def test_the_root_reports_what_the_credential_may_do(self):
        with _serving():
            response = self.client.get(
                "/api/v1/", HTTP_AUTHORIZATION=f"Bearer {_token()}"
            )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["granted"], ["fitness.write"])
        self.assertEqual(data["actor"], "health-sync-shortcut")
        self.assertEqual(data["resource"], RESOURCE)

    def test_capabilities_flag_what_this_token_may_run(self):
        with _serving():
            response = self.client.get(
                "/api/v1/capabilities/", HTTP_AUTHORIZATION=f"Bearer {_token()}"
            )
        self.assertEqual(response.status_code, 200)
        specs = response.json()["data"]["capabilities"]
        self.assertTrue(specs)
        # Nothing in HQ core is grantable by `fitness.write` alone, so a token
        # scoped to it must not come back permitted for anything here.
        self.assertFalse([spec for spec in specs if spec["permitted"]])

    def test_a_response_is_never_cacheable(self):
        with _serving():
            response = self.client.get(
                "/api/v1/", HTTP_AUTHORIZATION=f"Bearer {_token()}"
            )
        self.assertEqual(response["Cache-Control"], "private, no-store")

    @override_settings(SEVERINO_API_RESOURCE="")
    def test_the_surface_is_off_until_a_resource_is_configured(self):
        response = self.client.get("/api/v1/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "not_configured")
