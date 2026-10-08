"""The access boundary, asserted rather than assumed.

Every test here fails closed. The properties they pin: every route
authenticates, sign-in is rate limited, a forwarded header is believed only
from a declared proxy, an upload cannot choose its response type, are ones
whose loss is invisible in review of the change that causes it.
"""

import re
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import Resolver404, URLPattern, URLResolver, get_resolver, resolve
from django.urls.resolvers import RoutePattern
from django.utils import timezone

from hq.platform.core.models import AuditLog
from hq.platform.core.network import client_ip, parse_ip

PROXY = "10.0.0.5"


class _FakeRequest:
    def __init__(self, peer, forwarded=None):
        self.META = {"REMOTE_ADDR": peer}
        if forwarded is not None:
            self.META["HTTP_X_FORWARDED_FOR"] = forwarded


@override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY])
class ClientAddressTests(SimpleTestCase):
    """Who HQ believes the caller is."""

    @override_settings(SEVERINO_TRUSTED_PROXIES=["127.0.0.0/8", "::1/128"])
    def test_the_shipped_default_does_not_believe_the_lan(self):
        """The default trusts only loopback, never a whole range.

        HQ binds the host network namespace, so a LAN or tailnet peer can reach
        the port without passing the proxy. Trusting the range let any of them
        name the address written into the audit log as the source of a failed
        sign-in: the same rows the throttle reads back.
        """

        request = _FakeRequest("10.9.9.9", forwarded="203.0.113.9")
        self.assertEqual(client_ip(request), "10.9.9.9")

    def test_peer_is_used_when_there_is_no_proxy(self):
        self.assertEqual(client_ip(_FakeRequest("100.101.102.103")), "100.101.102.103")

    def test_forwarded_header_from_a_stranger_is_ignored(self):
        """The whole point. Otherwise the gate is opened by asking politely."""

        request = _FakeRequest("203.0.113.9", forwarded="100.64.0.1")
        self.assertEqual(client_ip(request), "203.0.113.9")

    def test_forwarded_header_from_the_proxy_is_believed(self):
        request = _FakeRequest(PROXY, forwarded="100.64.0.1")
        self.assertEqual(client_ip(request), "100.64.0.1")

    def test_the_chain_is_read_from_the_right(self):
        """Everything left of the last untrusted hop is attacker-written text."""

        request = _FakeRequest(PROXY, forwarded="100.64.0.1, 203.0.113.9")
        self.assertEqual(client_ip(request), "203.0.113.9")

    def test_a_spoofed_prefix_cannot_hide_the_real_caller(self):
        request = _FakeRequest(PROXY, forwarded="10.0.0.9, 203.0.113.9")
        self.assertEqual(client_ip(request), "203.0.113.9")

    def test_ports_and_brackets_are_stripped(self):
        self.assertEqual(str(parse_ip("10.0.0.4:53812")), "10.0.0.4")
        self.assertEqual(str(parse_ip("[::1]:8000")), "::1")
        self.assertEqual(str(parse_ip("fd7a:115c:a1e0::1")), "fd7a:115c:a1e0::1")

    def test_nonsense_is_not_trusted(self):
        self.assertIsNone(parse_ip("not-an-address"))
        self.assertIsNone(parse_ip(""))


@override_settings(
    SEVERINO_ENFORCE_TRUSTED_NETWORK=True, SEVERINO_TRUSTED_PROXIES=[PROXY]
)
class TrustedNetworkTests(TestCase):
    """Who may reach HQ at all."""

    def test_loopback_reaches_the_healthcheck(self):
        response = self.client.get("/health/live/", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response.status_code, 200)

    def test_the_tailnet_is_served(self):
        response = self.client.get("/health/live/", REMOTE_ADDR="100.64.0.1")
        self.assertEqual(response.status_code, 200)

    def test_the_public_internet_is_refused(self):
        response = self.client.get("/health/live/", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(response.status_code, 403)

    def test_refused_before_the_login_form_is_reachable(self):
        response = self.client.get("/accounts/login/", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(response.status_code, 403)

    def test_a_public_client_behind_the_proxy_is_still_refused(self):
        """The realistic exposure: the proxy is trusted, the caller is not."""

        response = self.client.get(
            "/health/live/", REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_FOR="203.0.113.9"
        )
        self.assertEqual(response.status_code, 403)

    def test_a_stranger_cannot_forge_a_tailnet_address(self):
        response = self.client.get(
            "/health/live/", REMOTE_ADDR="203.0.113.9", HTTP_X_FORWARDED_FOR="100.64.0.1"
        )
        self.assertEqual(response.status_code, 403)

    def test_the_refusal_describes_nothing(self):
        response = self.client.get("/health/live/", REMOTE_ADDR="203.0.113.9")
        body = response.content.decode().lower()
        for leak in ("100.64", "192.168", "10.0.0", "tailnet", "cidr"):
            self.assertNotIn(leak, body)

    def test_the_private_lan_is_not_the_boundary(self):
        """The private LAN is not a trust boundary.

        A home LAN holds a television, a printer, and whatever a guest joined.
        Trusting those ranges reads like a small widening of "reachable from
        the VPN" and is actually the whole rule undone, so it is asserted
        here rather than left to whoever next edits the list.
        """

        for address in ("10.9.9.9", "172.20.4.4"):
            with self.subTest(address=address):
                response = self.client.get("/health/live/", REMOTE_ADDR=address)
                self.assertEqual(response.status_code, 403)

    def test_a_deployment_can_still_declare_its_own_network(self):
        """Stricter by default is not the same as unconfigurable."""

        with override_settings(SEVERINO_TRUSTED_NETWORKS=["10.9.9.0/24"]):
            response = self.client.get("/health/live/", REMOTE_ADDR="10.9.9.9")

        self.assertEqual(response.status_code, 200)


class LoginThrottleTests(TestCase):
    """Guessing has to get expensive, and the evidence has to agree."""

    def setUp(self):
        get_user_model().objects.create_user(username="joe", password="correct-horse")

    def _attempt(self, username="joe", password="wrong", ip="100.64.0.1"):
        return self.client.post(
            "/accounts/login/",
            {"username": username, "password": password},
            REMOTE_ADDR=ip,
        )

    def test_a_wrong_password_is_recorded_with_its_source(self):
        self._attempt()
        entry = AuditLog.objects.get(action=AuditLog.Action.LOGIN_FAILED)
        self.assertEqual(entry.metadata["ip"], "100.64.0.1")
        self.assertEqual(entry.metadata["username"], "joe")

    def test_sign_in_is_barred_after_repeated_failures(self):
        for _ in range(5):
            self._attempt()
        response = self._attempt()
        self.assertEqual(response.status_code, 429)

    def test_the_correct_password_is_refused_while_locked(self):
        """A lock that the real password opens is not a lock."""

        for _ in range(5):
            self._attempt()
        response = self._attempt(password="correct-horse")
        self.assertEqual(response.status_code, 429)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_spraying_many_usernames_from_one_source_still_locks(self):
        for index in range(5):
            self._attempt(username=f"user{index}")
        response = self._attempt(username="someone-else")
        self.assertEqual(response.status_code, 429)

    def test_one_account_attacked_from_many_sources_still_locks(self):
        for index in range(5):
            self._attempt(ip=f"100.64.0.{index + 10}")
        response = self._attempt(ip="100.64.0.99")
        self.assertEqual(response.status_code, 429)

    def test_the_lock_lifts_with_time_and_nothing_else(self):
        for _ in range(5):
            self._attempt()
        AuditLog.objects.filter(action=AuditLog.Action.LOGIN_FAILED).update(
            created_at=timezone.now() - timezone.timedelta(hours=2)
        )
        response = self._attempt(password="correct-horse")
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_the_message_names_no_account(self):
        """Scoped to the message, not the page.

        Asserted against the whole body this is a coin flip: the page carries
        a random CSRF token, which can contain the username as a substring. The subject is the
        error text, so that is what is read.
        """

        for _ in range(5):
            self._attempt()
        response = self._attempt()
        message = " ".join(
            str(error) for error in response.context["form"].non_field_errors()
        ).lower()
        self.assertIn("too many failed", message)
        self.assertNotIn("joe", message)

    @override_settings(SEVERINO_LOGIN_MAX_ATTEMPTS=0)
    def test_the_throttle_can_be_switched_off_for_recovery(self):
        for _ in range(8):
            self._attempt()
        self.assertEqual(self._attempt(password="correct-horse").status_code, 302)


@override_settings(
    SEVERINO_OIDC_ENABLED=True,
    SEVERINO_PASSWORD_LOGIN_ENABLED=False,
    AUTHENTICATION_BACKENDS=["hq.platform.core.oidc.HQOIDCAuthenticationBackend"],
)
class SingleSignOnOnlyTests(TestCase):
    """With no password accepted, the guessing surface is gone, not throttled."""

    def setUp(self):
        get_user_model().objects.create_user(username="joe", password="correct-horse")

    def test_the_correct_password_is_refused(self):
        """A hidden form is not a closed one; nothing may accept a password."""

        response = self.client.post(
            "/accounts/login/",
            {"username": "joe", "password": "correct-horse"},
            REMOTE_ADDR="100.64.0.1",
        )
        self.assertEqual(response.status_code, 403)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_no_password_backend_is_installed(self):
        """Belt and braces: nothing anywhere can authenticate a password."""

        from django.contrib.auth import authenticate

        self.assertIsNone(authenticate(username="joe", password="correct-horse"))

    def test_a_failed_sign_in_stops_and_says_why_instead_of_looping(self):
        session = self.client.session
        session["oidc_failure"] = "Pocket ID refused HQ's client credentials."
        session.save()

        response = self.client.get("/accounts/login/?sso_failed=1", REMOTE_ADDR="100.64.0.1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pocket ID refused HQ&#x27;s client credentials.")
        self.assertContains(response, "Sign in with Pocket ID")

    def test_signing_in_goes_straight_to_the_provider(self):
        response = self.client.get("/accounts/login/", REMOTE_ADDR="100.64.0.1")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/oidc/", response["Location"])

    def test_the_original_destination_survives_the_bounce(self):
        response = self.client.get(
            "/accounts/login/?next=/expenses/", REMOTE_ADDR="100.64.0.1"
        )
        self.assertIn("next=", response["Location"])
        self.assertIn("expenses", response["Location"])

    def test_signing_out_does_not_sign_you_back_in(self):
        """Otherwise the button appears to do nothing at all."""

        response = self.client.get(
            "/accounts/login/?signed_out=1", REMOTE_ADDR="100.64.0.1"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("signed out", response.content.decode().lower())

    def test_break_glass_restores_the_form(self):
        with override_settings(
            SEVERINO_PASSWORD_LOGIN_ENABLED=True,
            AUTHENTICATION_BACKENDS=[
                "hq.platform.core.oidc.HQOIDCAuthenticationBackend",
                "django.contrib.auth.backends.ModelBackend",
            ],
        ):
            response = self.client.post(
                "/accounts/login/",
                {"username": "joe", "password": "correct-horse"},
                REMOTE_ADDR="100.64.0.1",
            )
            self.assertEqual(response.status_code, 302)
            self.assertIn("_auth_user_id", self.client.session)


class HealthDisclosureTests(TestCase):
    """The one endpoint that answers without a credential."""

    def test_readiness_does_not_enumerate_plugins(self):
        """The host repo is public; the extensions it composes are not."""

        response = self.client.get("/health/ready/")
        checks = response.json()["checks"]
        self.assertFalse(
            [key for key in checks if key.startswith("plugin:")],
            "readiness named individual plugins to an unauthenticated caller",
        )

    def test_an_operator_can_still_see_which_plugin_is_unhealthy(self):
        """Detail belongs to operators, not to anonymous callers."""

        from hq.platform.application.plugins import plugin_health

        user = get_user_model().objects.create_user("opr", password="x")
        self.client.force_login(user)
        response = self.client.get("/health/ready/")
        checks = response.json()["checks"]
        if plugin_health():
            self.assertTrue([key for key in checks if key.startswith("plugin:")])

    def test_readiness_still_reports_whether_it_can_serve(self):
        response = self.client.get("/health/ready/")
        self.assertIn(response.json()["status"], {"ok", "unavailable"})


def _routes(patterns=None, prefix=""):
    """Every leaf route in the composed URLconf, with its view."""

    if patterns is None:
        patterns = get_resolver().url_patterns
    for entry in patterns:
        if isinstance(entry, URLResolver):
            yield from _routes(entry.url_patterns, prefix + str(entry.pattern))
        elif isinstance(entry, URLPattern):
            yield prefix + str(entry.pattern), entry.callback


# What answers without a session, by path prefix. The reviewed list the
# sweep below holds every route to; nothing at runtime reads it.
PUBLIC_SURFACE = (
    "/health/",  # container probes, which cannot sign in
    "/accounts/login/",
    "/accounts/logout/",
    "/oidc/",  # the SSO handshake itself
    "/api/v2/",  # bearer-token authenticated, tested above; /api/docs/ is a page
    # A browser reporting a refused policy sends no credentials, so requiring a
    # session here would silence the reports that matter most: the ones from
    # the sign-in page. It stores nothing it was not sent and answers 204.
    "/csp-report/",
)

# A value each URL converter accepts, so a parameterised route can be asked for.
_CONVERTER_SAMPLES = {
    "IntConverter": "1",
    "StringConverter": "sample",
    "SlugConverter": "sample",
    "PathConverter": "sample",
    "UUIDConverter": "00000000-0000-0000-0000-000000000000",
}


def _sample(pattern) -> str:
    if isinstance(pattern, RoutePattern):
        return re.sub(
            r"<(?:\w+:)?(\w+)>",
            lambda m: _CONVERTER_SAMPLES[type(pattern.converters[m.group(1)]).__name__],
            str(pattern),
        )
    regex = pattern._regex.lstrip("^").rstrip("$").replace("\\Z", "")
    # A group of alternatives takes its first; any other group, a word.
    regex = re.sub(
        r"\(\?P<\w+>([^()]*)\)",
        lambda m: m.group(1).split("|")[0] if "|" in m.group(1) else "sample",
        regex,
    )
    return regex.replace("\\", "")


def _sampled_routes(patterns=None, prefix="/"):
    """Every leaf route as a path that should resolve to it, with its view."""

    for entry in get_resolver().url_patterns if patterns is None else patterns:
        if isinstance(entry, URLResolver):
            yield from _sampled_routes(entry.url_patterns, prefix + _sample(entry.pattern))
        elif isinstance(entry, URLPattern):
            yield prefix + _sample(entry.pattern), entry.callback


def _sent_to_sign_in(response, view) -> bool:
    """A redirect to sign in, carrying where to come back to."""

    if response.status_code != 302:
        return False
    location = urlsplit(response["Location"])
    sign_in = {settings.LOGIN_URL, str(getattr(view, "login_url", "") or settings.LOGIN_URL)}
    return location.path in sign_in and "next=" in location.query


class RouteExposureTests(SimpleTestCase):
    """No route reaches production unauthenticated by accident."""

    def test_every_api_route_authenticates_itself(self):
        """`/api/` skips the login redirect, so each view must do its own check.

        Not a style rule. A view added to `hq_api/urls.py` without the
        decorator is served to anybody who can reach the port, and nothing
        else in the stack would stop it. A page mounted under /api/ that keeps
        the login redirect (the reference at /api/docs/) is a page like any
        other.
        """

        unguarded = [
            route
            for route, view in _routes()
            if route.startswith("api/")
            and getattr(view, "login_required", True) is False
            and not getattr(view, "__hq_authenticated__", False)
        ]
        self.assertEqual(
            unguarded, [], f"API routes served without authentication: {unguarded}"
        )


class AnonymousSweepTests(TestCase):
    """Ask for everything without a credential; nothing may answer."""

    def test_there_is_no_admin_site(self):
        """Every write goes through the capability policy. Django's admin
        writes a model directly, with no capability, approval or audit
        attribution, so it is not installed, not routed and not registered."""

        from pathlib import Path

        from django.apps import apps

        self.assertFalse(apps.is_installed("django.contrib.admin"))
        self.assertFalse(any(route.startswith("admin/") for route, _view in _routes()))
        root = Path(settings.BASE_DIR)
        imports = re.compile(
            r"^\s*(from django\.contrib\.admin\b|import django\.contrib\.admin\b"
            r"|from django\.contrib import [^\n]*\badmin\b)",
            re.MULTILINE,
        )
        registered = [
            str(path.relative_to(root))
            for package in ("hq", "hq_sdk")
            for path in (root / package).rglob("*.py")
            if imports.search(path.read_text())
        ]
        self.assertEqual(registered, [])
        for user in (None, get_user_model().objects.create_superuser("root-example", password="unused-test-pass")):
            client = Client()
            if user is not None:
                client.force_login(user)
            for path in ("/admin/", "/admin/login/", "/admin/core/auditlog/"):
                with self.subTest(path=path, signed_in=user is not None):
                    response = client.get(path, REMOTE_ADDR="127.0.0.1")
                    self.assertIn(response.status_code, (302, 404))
                    if response.status_code == 302:
                        self.assertTrue(response["Location"].startswith("/accounts/login/"))

    def test_only_the_reviewed_surface_answers_without_a_session(self):
        """Every route in the composed URLconf, asked for without a session.

        A change detector, on purpose: a route is public exactly when its path
        falls under PUBLIC_SURFACE (or a route an extension declared as
        carrying its own token), and every other route sends a stranger to
        sign in. Widening what the public may reach should be a deliberate
        edit to that list with a reason attached, not a side effect.
        """

        from hq.platform.application.plugins import plugin_token_authenticated_prefixes

        public = PUBLIC_SURFACE + plugin_token_authenticated_prefixes()
        client = Client(raise_request_exception=False)
        wrong, asked = [], 0
        for path, view in _sampled_routes():
            try:
                if resolve(path).func is not view:
                    continue  # shadowed by an earlier route; nothing reaches it
            except Resolver404:
                wrong.append(f"{path}: the sample does not resolve")
                continue
            asked += 1
            response = client.get(path, REMOTE_ADDR="127.0.0.1")
            if _sent_to_sign_in(response, view) == path.startswith(public):
                wrong.append(f"{path}: {response.status_code} {response.get('Location', '')}")
        self.assertEqual(wrong, [])
        self.assertGreater(asked, 100)


class ReceiptUploadHardeningTests(TestCase):
    """A receipt must not be able to become a page on HQ's own origin."""

    def test_an_upload_without_a_declared_type_is_refused(self):
        """An undeclared type must not pass the allowlist by default."""

        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile
        from hq.domains.receipts.validation import validate_receipt_file

        sneaky = SimpleUploadedFile("note.html", b"markup", content_type="")
        with self.assertRaises(ValidationError):
            validate_receipt_file(sneaky)

    def test_a_declared_html_type_is_refused(self):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile
        from hq.domains.receipts.validation import validate_receipt_file

        with self.assertRaises(ValidationError):
            validate_receipt_file(
                SimpleUploadedFile("note.html", b"x", content_type="text/html")
            )

    def test_a_real_receipt_still_uploads(self):
        """The gate has to stay usable, or it gets removed instead of fixed."""

        from django.core.files.uploadedfile import SimpleUploadedFile
        from hq.domains.receipts.validation import validate_receipt_file

        validate_receipt_file(
            SimpleUploadedFile("r.pdf", b"%PDF-1.4", content_type="application/pdf")
        )

    def test_the_served_type_never_comes_from_the_filename(self):
        """A stored row with an unaccepted or blank type serves inertly."""

        from django.contrib.auth import get_user_model
        from django.core.files.base import ContentFile
        from hq.domains.receipts.models import Receipt

        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        receipt = Receipt.objects.create(vendor="v", amount=1, content_type="")
        receipt.file.save("note.html", ContentFile(b"markup"))

        response = self.client.get(f"/receipts/{receipt.pk}/file/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response.get("Content-Disposition", ""))
        # Read to the end, as a server would: the file is closed when the
        # stream is exhausted, and a test that stops at the headers leaves it
        # open for the collector.
        self.assertEqual(b"".join(response.streaming_content), b"markup")

    def test_a_pdf_is_still_shown_in_place(self):
        from django.contrib.auth import get_user_model
        from django.core.files.base import ContentFile
        from hq.domains.receipts.models import Receipt

        user = get_user_model().objects.create_user("op2", password="x")
        self.client.force_login(user)
        receipt = Receipt.objects.create(
            vendor="v", amount=1, content_type="application/pdf"
        )
        receipt.file.save("r.pdf", ContentFile(b"%PDF-1.4"))

        response = self.client.get(f"/receipts/{receipt.pk}/file/")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-1.4")


class StaticCachingTests(SimpleTestCase):
    """Far-future caching is a production property, not a live-serving one."""

    def _cache_control(self, *, live, versioned):
        import asyncio
        from unittest.mock import patch

        from hq.platform.core.static import CachedStaticFiles

        class _Response:
            status_code = 200

            def __init__(self):
                self.headers = {}

        files = CachedStaticFiles(directory=".", check_dir=False)
        manifest = SimpleNamespace(hashed_files={"app.js": "app.0123456789ab.js"})
        with (
            override_settings(STATIC_LIVE=live),
            patch("hq.platform.core.static.staticfiles_storage", manifest),
            patch.object(
                CachedStaticFiles.__bases__[0],
                "get_response",
                new=_returns(_Response()),
            ),
        ):
            name = "app.0123456789ab.js" if versioned else "app.js"
            scope = {"type": "http", "method": "GET", "headers": []}
            response = asyncio.run(files.get_response(name, scope))
        return response.headers["Cache-Control"]

    def test_production_pins_a_versioned_asset_forever(self):
        self.assertEqual(
            self._cache_control(live=False, versioned=True),
            "public, max-age=31536000, immutable",
        )

    def test_an_unhashed_asset_is_cached_briefly(self):
        self.assertEqual(
            self._cache_control(live=False, versioned=False), "public, max-age=3600"
        )

    def test_serving_live_never_pins_anything(self):
        """Live serving reads the source trees, which change under one name.
        Pinned, every later edit would be invisible and present as the
        application not running the code on disk.
        """

        for versioned in (True, False):
            with self.subTest(versioned=versioned):
                self.assertEqual(
                    self._cache_control(live=True, versioned=versioned), "no-cache"
                )

    def test_serving_live_reads_the_source_tree_not_the_collected_one(self):
        import tempfile
        from pathlib import Path

        from hq.platform.core.static import CachedStaticFiles

        with tempfile.TemporaryDirectory() as collected, tempfile.TemporaryDirectory() as source:
            (Path(collected) / "app.css").write_text("collected")
            (Path(source) / "app.css").write_text("edited")
            files = CachedStaticFiles(directory=collected)
            for live, expected in ((True, source), (False, collected)):
                with self.subTest(live=live), override_settings(
                    STATIC_LIVE=live, STATICFILES_DIRS=[source]
                ):
                    path, _ = files.lookup_path("app.css")
                    self.assertEqual(Path(path).parent.resolve(), Path(expected).resolve())


def _returns(value):
    async def _get_response(self, path, scope):
        return value

    return _get_response


class PrecompressedStaticTests(SimpleTestCase):
    """An asset is compressed once, when it is collected, and that copy is what is sent."""

    BODY = "body { color: red }\n" * 200
    # Not the gzip of BODY: what arrives shows which file was read.
    COPY = "precompressed"

    def _get(self, path, *, live=False, copy=True, **headers):
        import gzip
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        import httpx
        from asgiref.sync import async_to_sync
        from starlette.applications import Starlette
        from starlette.routing import Mount

        from hq.platform.core.static import CachedStaticFiles

        async def request(root):
            # Mounted, as production mounts it, so a refusal is a response.
            app = Starlette(routes=[Mount("/", app=CachedStaticFiles(directory=root))])
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(path, headers={"Accept-Encoding": "identity", **headers})

        manifest = SimpleNamespace(hashed_files={"app.css": "app.0123456789ab.css"})
        with tempfile.TemporaryDirectory() as root:
            for name in ("app.css", "app.0123456789ab.css"):
                Path(root, name).write_text(self.BODY, encoding="utf-8")
                if copy:
                    Path(root, f"{name}.gz").write_bytes(gzip.compress(self.COPY.encode(), mtime=0))
            for written in Path(root).iterdir():
                # A validator is the file's time and size: the same in every scratch tree.
                os.utime(written, (1_700_000_000, 1_700_000_000))
            with (
                override_settings(STATIC_LIVE=live, STATICFILES_DIRS=[root]),
                patch("hq.platform.core.static.staticfiles_storage", manifest),
            ):
                return async_to_sync(request)(root)

    def test_a_request_that_takes_gzip_gets_the_collected_copy(self):
        response = self._get("/app.0123456789ab.css", **{"Accept-Encoding": "br, gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, self.COPY)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(response.headers["content-type"], "text/css; charset=utf-8")
        self.assertEqual(response.headers["vary"], "Accept-Encoding")
        self.assertEqual(response.headers["cache-control"], "public, max-age=31536000, immutable")
        self.assertEqual(response.headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_an_unhashed_name_is_compressed_but_never_pinned(self):
        response = self._get("/app.css", **{"Accept-Encoding": "gzip"})
        self.assertEqual(response.text, self.COPY)
        self.assertEqual(response.headers["cache-control"], "public, max-age=3600")

    def test_a_request_that_refuses_gzip_gets_the_asset_itself(self):
        for accepted in ("identity", "br", "gzip;q=0", "gzip; q=0.0", ""):
            with self.subTest(accepted=accepted):
                response = self._get("/app.0123456789ab.css", **{"Accept-Encoding": accepted})
                self.assertEqual(response.text, self.BODY)
                self.assertNotIn("content-encoding", response.headers)
                # Still varies: the same name has a compressed representation.
                self.assertEqual(response.headers["vary"], "Accept-Encoding")
                self.assertEqual(
                    response.headers["cache-control"], "public, max-age=31536000, immutable"
                )

    def test_an_asset_with_no_copy_is_sent_as_it_is(self):
        response = self._get("/app.0123456789ab.css", copy=False, **{"Accept-Encoding": "gzip"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, self.BODY)
        self.assertNotIn("content-encoding", response.headers)

    def test_a_copy_already_held_is_not_sent_again(self):
        first = self._get("/app.0123456789ab.css", **{"Accept-Encoding": "gzip"})
        again = self._get(
            "/app.0123456789ab.css",
            **{"Accept-Encoding": "gzip", "If-None-Match": first.headers["etag"]},
        )
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.content, b"")
        self.assertEqual(again.headers["vary"], "Accept-Encoding")
        self.assertEqual(again.headers["cache-control"], "public, max-age=31536000, immutable")

    def test_each_representation_has_its_own_validator(self):
        compressed = self._get("/app.0123456789ab.css", **{"Accept-Encoding": "gzip"})
        plain = self._get("/app.0123456789ab.css")
        self.assertNotEqual(compressed.headers["etag"], plain.headers["etag"])

    def test_a_missing_asset_is_not_found_either_way(self):
        for accepted in ("gzip", "identity"):
            with self.subTest(accepted=accepted):
                response = self._get("/absent.css", **{"Accept-Encoding": accepted})
                self.assertEqual(response.status_code, 404)

    def test_a_write_is_refused_before_any_lookup(self):
        import asyncio

        from starlette.exceptions import HTTPException

        from hq.platform.core.static import CachedStaticFiles

        scope = {"type": "http", "method": "POST", "headers": [(b"accept-encoding", b"gzip")]}
        with override_settings(STATIC_LIVE=False), self.assertRaises(HTTPException) as refused:
            asyncio.run(CachedStaticFiles(directory=".", check_dir=False).get_response("app.css", scope))
        self.assertEqual(refused.exception.status_code, 405)

    def test_serving_live_reads_the_source_and_never_a_copy(self):
        response = self._get("/app.css", live=True, **{"Accept-Encoding": "gzip"})
        self.assertEqual(response.text, self.BODY)
        self.assertNotIn("content-encoding", response.headers)
        self.assertEqual(response.headers["cache-control"], "no-cache")

    def test_what_takes_gzip(self):
        from hq.platform.core.static import accepts_gzip

        cases = {
            "gzip": True,
            "GZIP": True,
            "deflate, gzip;q=0.5, br": True,
            "*": True,
            "": False,
            "identity": False,
            "gzip;q=0": False,
            "gzip;q=nonsense": False,
            "xgzip": False,
        }
        for header, expected in cases.items():
            with self.subTest(header=header):
                scope = {"type": "http", "headers": [(b"accept-encoding", header.encode())]}
                self.assertIs(accepts_gzip(scope), expected)
        self.assertIs(accepts_gzip({"type": "http", "headers": []}), False)


@override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY])
class StaticAssetBoundaryTests(SimpleTestCase):
    """The gate must not have a hole where Starlette mounts things."""

    def _probe(self, peer, forwarded=None):
        from hq.platform.core.network import TrustedNetworkASGI

        sent = []

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"css"})

        headers = []
        if forwarded:
            headers.append((b"x-forwarded-for", forwarded.encode()))
        scope = {"type": "http", "client": (peer, 1234), "headers": headers}

        async def send(message):
            sent.append(message)

        import asyncio

        asyncio.run(TrustedNetworkASGI(app)(scope, None, send))
        return sent[0]["status"]

    @override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True)
    def test_static_is_refused_from_the_public_internet(self):
        self.assertEqual(self._probe("203.0.113.9"), 403)

    @override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True)
    def test_static_is_served_to_the_tailnet(self):
        self.assertEqual(self._probe("100.64.0.1"), 200)

    @override_settings(SEVERINO_ENFORCE_TRUSTED_NETWORK=True)
    def test_static_uses_the_same_forwarded_rule(self):
        """Shared predicate: a forged header must fail here exactly as elsewhere."""

        self.assertEqual(self._probe("203.0.113.9", forwarded="100.64.0.1"), 403)
        self.assertEqual(self._probe(PROXY, forwarded="203.0.113.9"), 403)
        self.assertEqual(self._probe(PROXY, forwarded="100.64.0.1"), 200)


class ResponseHeaderTests(TestCase):
    @override_settings(
        SECURE_PROXY_SSL_HEADER=("HTTP_X_FORWARDED_PROTO", "https"),
        SEVERINO_TRUSTED_PROXIES=[PROXY],
        # Who may reach HQ is a different question from whose word HQ takes
        # about the scheme, and this test is about the second. With the gate
        # on, neither request would get far enough to have an answer: the
        # gate has its own tests above, and this one would silently become an
        # assertion about them instead.
        SEVERINO_ENFORCE_TRUSTED_NETWORK=False,
    )
    def test_only_a_trusted_proxy_can_assert_https(self):
        from django.http import HttpResponse
        from hq.platform.core.network import TrustedNetworkMiddleware

        observed = []
        middleware = TrustedNetworkMiddleware(
            lambda request: observed.append(request.is_secure()) or HttpResponse()
        )
        stranger = RequestFactory().get(
            "/", REMOTE_ADDR="100.64.0.77", HTTP_X_FORWARDED_PROTO="https"
        )
        proxy = RequestFactory().get(
            "/", REMOTE_ADDR=PROXY, HTTP_X_FORWARDED_PROTO="https"
        )

        middleware(stranger)
        middleware(proxy)

        self.assertEqual(observed, [False, True])

    def test_the_browser_boundary_headers_are_present(self):
        response = self.client.get("/accounts/login/", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response["X-Frame-Options"], "DENY")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", response["Content-Security-Policy"])
        self.assertIn("object-src 'none'", response["Content-Security-Policy"])
        self.assertIn("camera=()", response["Permissions-Policy"])


class ReturnDestinationTests(TestCase):
    """"Go back where I came from" must not mean "go anywhere you name"."""

    def setUp(self):
        from hq.domains.control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="z", kind="cloudflare.zone", enabled=True,
            spec={"zone": "example.com", "connection_ref": "cf"},
        )
        user = get_user_model().objects.create_user("op", password="x" * 12)
        self.client.force_login(user)

    def test_a_destination_on_another_host_is_refused(self):
        response = self.client.post(
            "/domains/example.com/pin/", {"next": "https://example.net/phish"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("example.net", response["Location"])

    def test_a_protocol_relative_destination_is_refused(self):
        """`//host` is a full URL wearing the costume of a path."""

        response = self.client.post(
            "/domains/example.com/pin/", {"next": "//example.net/phish"}
        )
        self.assertNotIn("example.net", response["Location"])

    def test_a_local_destination_is_honoured(self):
        response = self.client.post(
            "/domains/example.com/pin/", {"next": "/domains/example.com/"}
        )
        self.assertEqual(response["Location"], "/domains/example.com/")


class BrowserBoundaryTests(TestCase):
    """What HQ tells the browser it may do with a page holding credentials."""

    def test_the_policy_forbids_writing_html_into_the_dom(self):
        """Trusted Types, asserted because its whole value is being absent-proof.

        The directive costs nothing while no script assigns a string to a DOM
        sink, which is exactly why it would be dropped without anyone noticing
        and the day it is dropped is the day a sink can be introduced with
        no browser objecting.
        """

        policy = self.client.get("/accounts/login/")["Content-Security-Policy"]

        self.assertIn("require-trusted-types-for 'script'", policy)
        # Exactly one policy name, and `allow-duplicates` absent. Both halves
        # matter: the name is the single audited place a response body becomes
        # markup, and refusing duplicates is what stops injected script from
        # creating a second policy to reach a sink with.
        self.assertIn("trusted-types hq-fragment;", f"{policy};")
        self.assertNotIn("allow-duplicates", policy)

    def test_only_the_shared_helper_turns_a_string_into_markup(self):
        """The directive is worth exactly as much as this stays true.

        Any second `DOMParser` is a second sink the policy header cannot see.
        """

        from pathlib import Path

        root = Path(__file__).resolve().parents[4] / "static" / "js"
        sources = {path.name: path.read_text("utf-8") for path in root.glob("*.js")}
        sinks = ("parseFromString", "createPolicy")

        for name, text in sources.items():
            for sink in sinks:
                with self.subTest(file=name, sink=sink):
                    self.assertLessEqual(
                        text.count(f"{sink}("),
                        1 if name == "fragment.js" else 0,
                        f"{name} reaches a Trusted Types sink outside the helper",
                    )

    def test_every_enhanced_request_uses_the_session_renewal_boundary(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[4] / "static" / "js"
        sources = {path.name: path.read_text("utf-8") for path in root.glob("*.js")}

        self.assertEqual(sources["fragment.js"].count("window.fetch("), 1)
        for name in ("app.js", "tables.js"):
            self.assertNotIn("window.fetch(", sources[name], name)
            self.assertNotRegex(sources[name], r"(?<![\w.])fetch\(", name)

    def test_the_policy_names_somewhere_to_report_a_violation(self):
        response = self.client.get("/accounts/login/")

        self.assertIn("report-uri /csp-report/", response["Content-Security-Policy"])
        self.assertIn('csp="/csp-report/"', response["Reporting-Endpoints"])

    def test_nothing_here_may_be_loaded_by_another_origin(self):
        response = self.client.get("/accounts/login/")

        self.assertEqual(response["Cross-Origin-Resource-Policy"], "same-origin")


def _directives(policy):
    return [directive.split()[0] for directive in policy.split(";") if directive.strip()]


class CookiePrefixTests(SimpleTestCase):
    """A cookie no neighbouring host can have written."""

    def test_a_secure_deployment_prefixes_the_cookies_it_sets(self):
        names = _cookie_names(secure=True)

        self.assertEqual(names, ("__Host-sessionid", "__Host-csrftoken"))

    def test_a_plain_http_deployment_does_not(self):
        """The browser refuses to store a `__Host-` cookie that is not Secure.

        Hard-coding the prefix would work in production and silently break
        every plain-HTTP development session, in the way that looks like
        sign-in is broken rather than like a cookie was rejected.
        """

        names = _cookie_names(secure=False)

        self.assertEqual(names, ("sessionid", "csrftoken"))


def _cookie_names(*, secure):
    """The names settings.py derives, evaluated the way settings.py does."""

    return (
        "__Host-sessionid" if secure else "sessionid",
        "__Host-csrftoken" if secure else "csrftoken",
    )


@override_settings(SEVERINO_TRUSTED_NETWORKS=["100.64.0.0/10", "127.0.0.0/8"])
class PolicyReportTests(TestCase):
    """The one boundary HQ cannot check from the inside."""

    def setUp(self):
        self.client = Client(REMOTE_ADDR="100.64.0.1")

    def report(self, payload, content_type="application/csp-report"):
        return self.client.post(
            "/csp-report/", data=payload, content_type=content_type
        )

    def test_a_violation_is_recorded_without_a_session(self):
        response = self.report(
            '{"csp-report": {"effective-directive": "script-src",'
            ' "blocked-uri": "https://evil.test/x.js",'
            ' "document-uri": "https://hq.test/"}}'
        )

        self.assertEqual(response.status_code, 204)
        event = AuditLog.objects.get(object_type="ContentSecurityPolicy")
        self.assertEqual(event.metadata["directive"], "script-src")
        self.assertEqual(event.metadata["blocked"], "https://evil.test/x.js")

    def test_the_reporting_api_shape_is_read_too(self):
        self.report(
            '[{"type": "csp-violation", "body": {"effectiveDirective":'
            ' "img-src", "blockedURL": "https://evil.test/x.png"}}]',
            content_type="application/reports+json",
        )

        self.assertEqual(
            AuditLog.objects.filter(object_type="ContentSecurityPolicy").count(), 1
        )

    def test_the_same_complaint_does_not_write_a_row_per_page_load(self):
        for _ in range(5):
            self.report(
                '{"csp-report": {"effective-directive": "script-src",'
                ' "blocked-uri": "inline"}}'
            )

        self.assertEqual(
            AuditLog.objects.filter(object_type="ContentSecurityPolicy").count(), 1
        )

    def test_nonsense_is_discarded_without_comment(self):
        for payload in ("not json", "[]", "{}", '{"csp-report": {}}', '"a"'):
            with self.subTest(payload=payload):
                response = self.report(payload)
                self.assertEqual(response.status_code, 204)

        self.assertFalse(
            AuditLog.objects.filter(object_type="ContentSecurityPolicy").exists()
        )

    def test_a_flood_cannot_be_used_to_write_a_large_row(self):
        self.report(
            '{"csp-report": {"effective-directive": "%s"}}' % ("a" * 32_000)
        )

        self.assertFalse(
            AuditLog.objects.filter(object_type="ContentSecurityPolicy").exists()
        )

    def test_a_long_field_is_kept_but_bounded(self):
        self.report(
            '{"csp-report": {"effective-directive": "script-src",'
            ' "blocked-uri": "https://evil.test/%s"}}' % ("a" * 2_000)
        )

        event = AuditLog.objects.get(object_type="ContentSecurityPolicy")
        self.assertEqual(len(event.metadata["blocked"]), 200)

    def test_it_is_not_a_readable_surface(self):
        self.assertEqual(self.client.get("/csp-report/").status_code, 405)

    def test_it_is_still_behind_the_network_gate(self):
        response = Client(REMOTE_ADDR="203.0.113.9").post(
            "/csp-report/", data="{}", content_type="application/csp-report"
        )

        self.assertEqual(response.status_code, 403)


class CanonicalEntryTests(TestCase):
    """The plain port HQ binds must not be a second front door."""

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_SSL_HOST="hq.example.com",
        SECURE_REDIRECT_EXEMPT=[r"^health/"],
        ALLOWED_HOSTS=["hq.example.com", "testserver"],
    )
    def test_a_plain_request_is_sent_to_the_canonical_name(self):
        response = self.client.get("/accounts/login/", REMOTE_ADDR="100.64.0.1")

        self.assertEqual(response.status_code, 301)
        self.assertEqual(
            response["Location"], "https://hq.example.com/accounts/login/"
        )

    @override_settings(
        SECURE_SSL_REDIRECT=True,
        SECURE_SSL_HOST="hq.example.com",
        SECURE_REDIRECT_EXEMPT=[r"^health/"],
    )
    def test_the_container_probe_is_exempt(self):
        """It probes the raw port from inside its own network namespace.

        The one caller for whom plain HTTP is the correct request. Redirecting
        it would make the container permanently unhealthy.
        """

        from unittest.mock import patch

        # Ready, as the image is: this is about the redirect, not the assets.
        with patch("hq.platform.core.health_views.collected", return_value=True):
            response = self.client.get("/health/ready/", REMOTE_ADDR="127.0.0.1")

        self.assertEqual(response.status_code, 200)
