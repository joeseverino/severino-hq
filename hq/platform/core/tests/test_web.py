"""The web shell's gate: who gets in, the security boundary, OIDC, protected files and what every response carries."""

import asyncio
import json
import logging
import re
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.templatetags.static import static
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from hq.domains.receipts.models import Receipt
from hq.platform.application import request_context
from hq.platform.application.tests.markup import hrefs
from hq.platform.core.audit import record_event
from hq.platform.core.logging import JsonFormatter, set_request_id
from hq.platform.core.oidc import TAILSCALE_PRINCIPAL_SESSION_KEY, HQOIDCAuthenticationBackend

from .signed_in_case import User, _AuthedTestCase


class AuthGateTests(TestCase):
    @patch("hq.platform.core.health_views.collected", return_value=True)
    def test_health_probes_are_anonymous_and_operational(self, _collected):
        live = self.client.get("/health/live/")
        ready = self.client.get("/health/ready/")

        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json(), {"status": "ok"})
        self.assertEqual(ready.status_code, 200)
        payload = ready.json()
        self.assertEqual(payload["status"], "ok")
        # The host's own checks must be present and passing. Asserted as a
        # subset, not as the whole dictionary: every admitted extension
        # contributes a `plugin:<id>` check of its own, so an equality here is
        # an assertion that nothing else is installed. That is true in this
        # repository's CI, which loads no extension, and false in the composed
        # image, which is the only place it matters: it passed every gate and
        # failed the composition.
        #
        # What this test is for is that readiness answers anonymously and
        # reports the host as healthy. It is not for how many extensions
        # happen to be composed alongside it.
        for name in ("database", "migrations", "storage", "assets"):
            self.assertIs(payload["checks"].get(name), True, name)
        self.assertTrue(
            all(payload["checks"].values()),
            f"a readiness check reported unhealthy: {payload['checks']}",
        )

    @override_settings(MEDIA_ROOT=Path("/path/that/does/not/exist"))
    def test_readiness_fails_closed_when_required_storage_is_missing(self):
        response = self.client.get("/health/ready/")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "unavailable")
        self.assertFalse(response.json()["checks"]["storage"])

    def test_readiness_fails_closed_without_collected_assets(self):
        """A start collects nothing, so an image built without its assets is not ready."""

        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(
                DEBUG=False,
                STATIC_LIVE=False,
                STATIC_ROOT=root,
                STORAGES={
                    **settings.STORAGES,
                    "staticfiles": {
                        **settings.STORAGES["staticfiles"],
                        "BACKEND": "hq.platform.core.static.HashedStaticStorage",
                    },
                },
            ),
        ):
            empty = self.client.get("/health/ready/")
            call_command("collectstatic", interactive=False, verbosity=0)
            collected = self.client.get("/health/ready/")
            modes = {path.stat().st_mode & 0o777 for path in Path(root).rglob("*") if path.is_file()}

        self.assertEqual(empty.status_code, 503)
        self.assertIs(empty.json()["checks"]["assets"], False)
        self.assertIs(collected.json()["checks"]["assets"], True)
        self.assertEqual(collected.status_code, 200)
        # Root collects into the image and another account serves from it.
        self.assertEqual(modes, {0o644})

    @override_settings(STATIC_LIVE=True)
    def test_serving_live_needs_nothing_collected(self):
        with patch("hq.platform.core.health_views.collected", return_value=False):
            response = self.client.get("/health/ready/")
        self.assertIs(response.json()["checks"]["assets"], True)

    def test_anonymous_dashboard_redirects_to_login_with_the_complete_destination(self):
        response = self.client.get("/?filter=open&next=https://example.com")

        self.assertEqual(response.status_code, 302)
        location = urlsplit(response["Location"])
        self.assertEqual(location.path, "/accounts/login/")
        self.assertEqual(parse_qs(location.query)["next"], ["/?filter=open&next=https://example.com"])

    def test_login_page_is_accessible(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_every_response_says_how_long_the_application_took(self):
        with self.assertLogs("severino.request", "INFO") as logged:
            response = self.client.get("/accounts/login/")
        self.assertRegex(response["Server-Timing"], r"^app;dur=\d+(\.\d+)?$")
        [record] = [record for record in logged.records if getattr(record, "event", "") == "http.request"]
        # One measurement: the header and the access log cannot disagree.
        self.assertEqual(response["Server-Timing"], f"app;dur={record.duration_ms}")
        self.assertEqual((record.levelname, record.getMessage()), ("INFO", "request completed"))
        self.assertIn("Server-Timing", self.client.get("/health/live/"))

    def test_a_slow_request_is_logged_as_a_warning(self):
        from hq.platform.core import middleware

        # Two readings of the clock, a threshold apart: the request's start and its end.
        clock = iter((100.0, 100.0 + middleware.SLOW_REQUEST_MS / 1000))
        with (
            patch.object(middleware, "monotonic", lambda: next(clock)),
            self.assertLogs("severino.request", "WARNING") as logged,
        ):
            response = self.client.get("/health/live/")
        [record] = logged.records
        self.assertEqual((record.levelname, record.getMessage()), ("WARNING", "slow request"))
        self.assertEqual((record.path, record.duration_ms), ("/health/live/", float(middleware.SLOW_REQUEST_MS)))
        self.assertEqual(response["Server-Timing"], f"app;dur={float(middleware.SLOW_REQUEST_MS)}")

    def test_a_probe_answered_in_time_is_not_logged(self):
        with self.assertNoLogs("severino.request", "INFO"):
            self.client.get("/health/live/")

    def test_style_attributes_are_the_only_relaxation_in_the_policy(self):
        """Pin the one exception so it stays one exception.

        A style element comes only from a sheet of HQ's own origin, as a
        script comes only from a tag HQ rendered. ``style-src-attr`` admits
        ``'unsafe-inline'`` only while WebKit reports a violation for every
        ``<select>`` (WebKitSelectCanaryTests says when it stops); HQ writes
        no style attribute (test_rendered, the template contract).

        Asserted rather than merely documented, because the failure mode of a
        documented rule is that a second relaxation quietly joins the first
        and the policy is weaker than the prose claims.
        """

        response = self.client.get("/accounts/login/")
        policy = response.headers["Content-Security-Policy"]
        directives = {
            directive.split()[0]: directive.split()[1:] for directive in policy.split(";") if directive.strip()
        }

        self.assertEqual(directives["default-src"], ["'self'"])
        nonce, *scripts = directives["script-src"]
        self.assertRegex(nonce, r"^'nonce-[^']+'$")
        self.assertEqual(scripts, ["'strict-dynamic'", "'self'"])
        self.assertEqual(directives["style-src-elem"], ["'self'"])
        self.assertEqual(directives["object-src"], ["'none'"])
        self.assertEqual(directives["frame-ancestors"], ["'none'"])
        relaxed = sorted(
            directive.split()[0]
            for directive in policy.split(";")
            if "'unsafe-inline'" in directive or "'unsafe-eval'" in directive
        )
        self.assertEqual(relaxed, ["style-src-attr"])

    def test_application_shell_names_every_asset_by_its_content(self):
        """Production's storage, collected: no static URL on the shell is plain."""

        # DEBUG off: with it on, Django's manifest storage hands out plain names.
        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(
                DEBUG=False,
                STATIC_ROOT=root,
                STORAGES={
                    **settings.STORAGES,
                    "staticfiles": {"BACKEND": "hq.platform.core.static.HashedStaticStorage"},
                },
            ),
        ):
            call_command("collectstatic", interactive=False, verbosity=0)
            content = self.client.get("/accounts/login/").content.decode()
        assets = re.findall(r'/static/([^"?]+)', content)
        self.assertTrue(assets)
        for asset in assets:
            with self.subTest(asset=asset):
                self.assertRegex(asset, r"\.[0-9a-f]{12}\.\w+$")

    def test_an_uncollected_asset_keeps_its_plain_name(self):
        """A checkout that never collected still renders, unversioned."""

        from django.contrib.staticfiles.storage import staticfiles_storage

        with (
            tempfile.TemporaryDirectory() as root,
            override_settings(
                DEBUG=False,
                STATIC_ROOT=root,
                STORAGES={
                    **settings.STORAGES,
                    "staticfiles": {"BACKEND": "hq.platform.core.static.HashedStaticStorage"},
                },
            ),
        ):
            self.assertEqual(staticfiles_storage.url("css/app.css"), "/static/css/app.css")

    @override_settings(SEVERINO_OIDC_ENABLED=True)
    def test_login_page_offers_single_sign_on_when_enabled(self):
        response = self.client.get("/accounts/login/")
        self.assertIn(reverse("oidc_authentication_init"), hrefs(response))


class SecurityBoundaryTests(TestCase):
    def test_an_audit_row_names_who_the_bound_request_signed_in(self):
        operator = User.objects.create_user("example-auditor")
        signed = RequestFactory().get("/")
        signed.user = operator
        anonymous = RequestFactory().get("/")
        anonymous.user = AnonymousUser()

        for request, expected in ((signed, operator), (anonymous, None), (None, None)):
            with self.subTest(request=request):
                with request_context.bind(request):
                    row = record_event(action="example.read", required=True)
                self.assertEqual(row.user, expected)

    def test_json_logs_carry_request_correlation_without_query_strings(self):
        with set_request_id("request-123"):
            record = logging.LogRecord(
                "severino.request",
                logging.INFO,
                __file__,
                1,
                "request completed",
                (),
                None,
            )
            record.event = "http.request"
            record.method = "GET"
            record.path = "/search/"
            record.status = 200
            record.duration_ms = 4.2
            payload = json.loads(JsonFormatter().format(record))

        self.assertEqual(payload["request_id"], "request-123")
        self.assertEqual(payload["path"], "/search/")
        self.assertEqual(payload["duration_ms"], 4.2)

    def test_request_user_attribution_is_isolated_between_async_tasks(self):
        async def exercise():
            ready = [asyncio.Event(), asyncio.Event()]
            release = asyncio.Event()

            async def worker(index, request):
                with request_context.bind(request):
                    ready[index].set()
                    await release.wait()
                    return request_context.current()

            tasks = [
                asyncio.create_task(worker(0, "first-request")),
                asyncio.create_task(worker(1, "second-request")),
            ]
            await asyncio.gather(*(event.wait() for event in ready))
            release.set()
            return await asyncio.gather(*tasks)

        self.assertEqual(async_to_sync(exercise)(), ["first-request", "second-request"])
        self.assertIsNone(request_context.current())


class OIDCBackendTests(TestCase):
    @patch("mozilla_django_oidc.auth.OIDCAuthenticationBackend.get_or_create_user")
    def test_verified_id_token_principal_is_bound_to_the_session(self, get_user):
        get_user.return_value = User(username="operator")
        backend = HQOIDCAuthenticationBackend()
        backend.request = type("Request", (), {"session": {}})()

        backend.get_or_create_user(
            "access-token",
            "id-token",
            {"tailscale_principal": " operator@passkey "},
        )

        self.assertEqual(
            backend.request.session[TAILSCALE_PRINCIPAL_SESSION_KEY],
            "operator@passkey",
        )

    @patch("mozilla_django_oidc.auth.OIDCAuthenticationBackend.get_or_create_user")
    def test_invalid_principal_claim_removes_a_stale_session_link(self, get_user):
        get_user.return_value = User(username="operator")
        backend = HQOIDCAuthenticationBackend()
        backend.request = type(
            "Request",
            (),
            {"session": {TAILSCALE_PRINCIPAL_SESSION_KEY: "stale@passkey"}},
        )()

        backend.get_or_create_user(
            "access-token",
            "id-token",
            {"tailscale_principal": ["operator@passkey"]},
        )

        self.assertNotIn(TAILSCALE_PRINCIPAL_SESSION_KEY, backend.request.session)

    @patch("mozilla_django_oidc.auth.OIDCAuthenticationBackend.get_or_create_user")
    def test_a_sign_in_links_the_github_account_it_claims(self, get_user):
        user = User.objects.create_user("operator")
        get_user.return_value = user
        backend = HQOIDCAuthenticationBackend()
        backend.request = type("Request", (), {"session": {}})()

        backend.get_or_create_user("access-token", "id-token", {"github": "example-user"})

        self.assertEqual(user.linked_accounts.get(provider="github").login, "example-user")

    @patch("mozilla_django_oidc.auth.OIDCAuthenticationBackend.authenticate")
    def test_a_refused_token_exchange_is_a_failed_sign_in_with_its_reason(self, authenticate):
        import requests

        refused = requests.Response()
        refused.status_code = 401
        authenticate.side_effect = requests.HTTPError("Get Token Error", response=refused)
        backend = HQOIDCAuthenticationBackend()
        request = type("Request", (), {"session": {}})()

        self.assertIsNone(backend.authenticate(request))
        self.assertIn("refused HQ's client credentials", request.session["oidc_failure"])

    def test_allows_user_in_allowed_group(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS={},
            SEVERINO_OIDC_ROLE_GROUPS={"admins": "operator"},
        ):
            self.assertTrue(
                backend.verify_claims(
                    {
                        "sub": "subject-1",
                        "preferred_username": "joe",
                        "groups": ["admins"],
                    }
                )
            )

    @override_settings(OIDC_ISSUER="https://id.example.test")
    def test_a_signed_in_subject_keeps_its_account_whatever_it_is_renamed_to(self):
        """Once bound, a name or address that moves to another person moves nothing."""

        first = User.objects.create_user(username="joe", email="joe@example.com")
        other = User.objects.create_user(username="sam", email="sam@example.com")
        backend = HQOIDCAuthenticationBackend()
        backend.request = type("Request", (), {"session": {}})()
        with patch(
            "mozilla_django_oidc.auth.OIDCAuthenticationBackend.get_or_create_user",
            return_value=first,
        ):
            backend.get_or_create_user("a", "i", {"sub": "subject-1", "preferred_username": "joe"})

        renamed = backend.filter_users_by_claims({"sub": "subject-1", "preferred_username": "sam"})
        impostor = backend.filter_users_by_claims(
            {"sub": "subject-2", "preferred_username": "joe", "email": "joe@example.com", "email_verified": True}
        )

        self.assertEqual(list(renamed), [first])
        self.assertEqual(list(impostor), [])
        self.assertEqual(
            list(
                backend.filter_users_by_claims({"sub": "subject-2", "email": "sam@example.com", "email_verified": True})
            ),
            [other],
        )

    def test_an_unverified_address_does_not_find_an_account(self):
        User.objects.create_user(username="joe", email="joe@example.com")
        backend = HQOIDCAuthenticationBackend()

        users = backend.filter_users_by_claims(
            {"sub": "subject-9", "preferred_username": "other", "email": "joe@example.com"}
        )

        self.assertEqual(list(users), [])

    def test_a_token_without_a_subject_is_not_a_sign_in(self):
        backend = HQOIDCAuthenticationBackend()
        backend.request = type("Request", (), {"session": {}})()

        with override_settings(SEVERINO_OIDC_ROLE_GROUPS={"admins": "operator"}):
            self.assertFalse(backend.verify_claims({"preferred_username": "joe", "groups": ["admins"]}))

    def test_allows_user_by_allowed_email(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS={"joe@example.com": "viewer"},
            SEVERINO_OIDC_ROLE_GROUPS={},
        ):
            self.assertTrue(
                backend.verify_claims(
                    {
                        "sub": "subject-1",
                        "email": "joe@example.com",
                        "email_verified": True,
                        "groups": [],
                    }
                )
            )

    def test_rejects_when_no_allow_rule_is_configured(self):
        backend = HQOIDCAuthenticationBackend()
        backend.request = type("Request", (), {"session": {}})()

        with (
            override_settings(
                SEVERINO_OIDC_ALLOWED_EMAILS={},
                SEVERINO_OIDC_ROLE_GROUPS={},
            ),
            self.assertRaises(PermissionDenied),
        ):
            backend.verify_claims(
                {
                    "sub": "subject-1",
                    "preferred_username": "joe",
                    "groups": ["admins"],
                }
            )


class ReceiptFileProtectionTests(_AuthedTestCase):
    def _make_receipt(self) -> Receipt:
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            r = Receipt.objects.create(
                vendor="Vendor",
                date=timezone.localdate(),
                amount=Decimal("10.00"),
                original_filename="test.txt",
                content_type="text/plain",
                size_bytes=5,
            )
            # Write a file directly to the storage path so the streaming view
            # has something to serve.
            path = Path(r.file.storage.location) / "hq/domains/receipts" / "test.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("hello", encoding="utf-8")
            r.file.name = "receipts/test.txt"
            r.save(update_fields=["file"])
            return r

    def test_anonymous_cannot_fetch_receipt(self):
        receipt = self._make_receipt()
        self.client.logout()
        r = self.client.get(f"/receipts/{receipt.pk}/file/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])


class CompressedResponseTests(TestCase):
    """A page big enough to compress reaches the server in the smallest coding
    the client takes, with one Content-Length that is the encoded body's: h11
    refuses a response carrying two, which would be a 502 on every large page."""

    def _fetch(self, accepted):
        import httpx2

        from hq.config.asgi import application

        async def fetch():
            transport = httpx2.ASGITransport(app=application, client=("100.64.0.10", 50000))
            async with (
                httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
                client.stream("GET", "/accounts/login/", headers={"Accept-Encoding": accepted}) as response,
            ):
                # As sent, not as the client would decode it.
                return response, b"".join([chunk async for chunk in response.aiter_raw()])

        return async_to_sync(fetch)()

    def test_a_large_page_arrives_compressed_with_one_length(self):
        import gzip

        import brotli

        # A browser's header over HTTPS, then a client of gzip alone, then none.
        for accepted, coding, decoded in (
            ("gzip, deflate, br, zstd", "br", brotli.decompress),
            ("gzip", "gzip", gzip.decompress),
            ("identity", None, bytes),
        ):
            with self.subTest(accepted=accepted):
                response, sent = self._fetch(accepted)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers.get("content-encoding"), coding)
                self.assertIn("Accept-Encoding", response.headers["vary"])
                self.assertEqual(len(response.headers.get_list("content-length")), 1)
                self.assertEqual(int(response.headers["content-length"]), len(sent))
                self.assertIn(b"<form", decoded(sent))


class RootIconTests(SimpleTestCase):
    """What browsers ask for at the root whatever a page links is answered,
    signed in or not, so no visit logs a 404 for an icon."""

    def test_each_root_icon_is_a_permanent_redirect_to_the_collected_file(self):
        for path, name in (
            ("/favicon.ico", "img/favicon.ico"),
            ("/apple-touch-icon.png", "img/apple-touch-icon.png"),
            ("/apple-touch-icon-precomposed.png", "img/apple-touch-icon.png"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 301)
                self.assertEqual(response["Location"], static(name))
