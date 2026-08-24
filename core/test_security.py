"""The access boundary, asserted rather than assumed.

Every test here fails closed. The properties they pin -- every route
authenticates, sign-in is rate limited, a forwarded header is believed only
from a declared proxy, an upload cannot choose its response type -- are ones
whose loss is invisible in review of the change that causes it.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import Client, RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import URLPattern, URLResolver, get_resolver
from django.utils import timezone

from core.models import AuditLog
from core.network import client_ip, parse_ip

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
        """The regression guard for a whole-range default.

        HQ binds the host network namespace, so a LAN or tailnet peer can reach
        the port without passing the proxy. Trusting the range let any of them
        name the address written into the audit log as the source of a failed
        sign-in -- the same rows the throttle reads back.
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

        Asserted against the whole body this was a coin flip: the page carries
        a random CSRF token, and a token happening to contain the username as a
        substring failed a test about what the message says. The subject is the
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
    AUTHENTICATION_BACKENDS=["core.oidc.HQOIDCAuthenticationBackend"],
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
                "core.oidc.HQOIDCAuthenticationBackend",
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

        from application.plugins import plugin_health

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


class RouteExposureTests(SimpleTestCase):
    """No route reaches production unauthenticated by accident."""

    def test_every_api_route_authenticates_itself(self):
        """`/api/` skips the login redirect, so each view must do its own check.

        Not a style rule. A view added to `hq_api/urls.py` without the
        decorator is served to anybody who can reach the port, and nothing
        else in the stack would stop it.
        """

        unguarded = [
            route
            for route, view in _routes()
            if route.startswith("api/")
            and not getattr(view, "__hq_authenticated__", False)
        ]
        self.assertEqual(
            unguarded, [], f"API routes served without authentication: {unguarded}"
        )

    def test_the_unauthenticated_surface_is_the_reviewed_one(self):
        """A change detector, on purpose.

        Widening what the public may reach should be a deliberate edit to this
        list with a reason attached, not a side effect of adding a prefix.
        """

        self.assertEqual(
            set(__import__("django.conf", fromlist=["settings"]).settings.LOGIN_EXEMPT_PATH_PREFIXES),
            {
                "/health/",  # container probes, which cannot sign in
                "/accounts/login",
                "/accounts/logout",
                "/oidc/",  # the SSO handshake itself
                "/static/",
                "/api/",  # bearer-token authenticated, tested above
            },
        )


class AnonymousSweepTests(TestCase):
    """Ask for everything without a credential; nothing may answer."""

    def test_the_admin_password_form_is_not_a_second_door(self):
        """One sign-in path, so one set of rules governs every attempt."""

        response = Client().get("/admin/login/", REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("/accounts/login/"))

    def test_no_page_serves_content_to_an_anonymous_caller(self):
        client = Client()
        served = []
        for route, _view in _routes():
            if any(
                route.startswith(prefix.lstrip("/"))
                for prefix in ("health/", "accounts/", "oidc/", "static/", "api/")
            ):
                continue
            if "<" in route or "(" in route:
                # Parameterised routes cannot be built without inventing an id;
                # they share the same middleware gate as the rest.
                continue
            response = client.get(f"/{route}", REMOTE_ADDR="127.0.0.1")
            if response.status_code == 200:
                served.append(route)
        self.assertEqual(served, [], f"served to an anonymous caller: {served}")


class ReceiptUploadHardeningTests(TestCase):
    """A receipt must not be able to become a page on HQ's own origin."""

    def test_an_upload_without_a_declared_type_is_refused(self):
        """An undeclared type must not pass the allowlist by default."""

        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile
        from receipts.validation import validate_receipt_file

        sneaky = SimpleUploadedFile("note.html", b"markup", content_type="")
        with self.assertRaises(ValidationError):
            validate_receipt_file(sneaky)

    def test_a_declared_html_type_is_refused(self):
        from django.core.exceptions import ValidationError
        from django.core.files.uploadedfile import SimpleUploadedFile
        from receipts.validation import validate_receipt_file

        with self.assertRaises(ValidationError):
            validate_receipt_file(
                SimpleUploadedFile("note.html", b"x", content_type="text/html")
            )

    def test_a_real_receipt_still_uploads(self):
        """The gate has to stay usable, or it gets removed instead of fixed."""

        from django.core.files.uploadedfile import SimpleUploadedFile
        from receipts.validation import validate_receipt_file

        validate_receipt_file(
            SimpleUploadedFile("r.pdf", b"%PDF-1.4", content_type="application/pdf")
        )

    def test_the_served_type_never_comes_from_the_filename(self):
        """A stored row with an unaccepted or blank type serves inertly."""

        from django.contrib.auth import get_user_model
        from django.core.files.base import ContentFile
        from receipts.models import Receipt

        user = get_user_model().objects.create_user("op", password="x")
        self.client.force_login(user)
        receipt = Receipt.objects.create(vendor="v", amount=1, content_type="")
        receipt.file.save("note.html", ContentFile(b"markup"))

        response = self.client.get(f"/receipts/{receipt.pk}/file/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", response.get("Content-Disposition", ""))

    def test_a_pdf_is_still_shown_in_place(self):
        from django.contrib.auth import get_user_model
        from django.core.files.base import ContentFile
        from receipts.models import Receipt

        user = get_user_model().objects.create_user("op2", password="x")
        self.client.force_login(user)
        receipt = Receipt.objects.create(
            vendor="v", amount=1, content_type="application/pdf"
        )
        receipt.file.save("r.pdf", ContentFile(b"%PDF-1.4"))

        response = self.client.get(f"/receipts/{receipt.pk}/file/")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertNotIn("attachment", response.get("Content-Disposition", ""))


@override_settings(SEVERINO_TRUSTED_PROXIES=[PROXY])
class StaticAssetBoundaryTests(SimpleTestCase):
    """The gate must not have a hole where Starlette mounts things."""

    def _probe(self, peer, forwarded=None):
        from core.network import TrustedNetworkASGI

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
    )
    def test_only_a_trusted_proxy_can_assert_https(self):
        from django.http import HttpResponse
        from core.network import TrustedNetworkMiddleware

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
        from control_plane.models import ManagedResource

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
