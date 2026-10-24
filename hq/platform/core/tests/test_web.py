"""The web shell: the auth gate, health, search, lists, the dashboard, the
manifest importer and the audit pages."""

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.templatetags.static import static
from django.test import Client, SimpleTestCase, TestCase, override_settings, tag
from django.urls import reverse
from django.utils import timezone

from hq.domains.assets.models import Asset
from hq.domains.content.models import ContentItem
from hq.domains.control_plane.models import (
    DashboardConfiguration,
    DashboardMachine,
    DashboardRefreshRequest,
    ManagedResource,
    ProviderConnection,
    ProviderInventory,
)
from hq.domains.docs_index.importer import (
    ManifestImportError,
    import_manifest_data,
    validate_manifest_data,
)
from hq.domains.docs_index.models import DocumentationRecord
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.domains.receipts.models import Receipt
from hq.platform.application import readings
from hq.platform.application.action_links import command_url
from hq.platform.application.dashboard import operating_snapshot
from hq.platform.application.security import cli_principal
from hq.platform.application.tests.host_only import host_only
from hq.platform.application.tests.markup import elements, hrefs, select, text, within
from hq.platform.core.audit import operation_context
from hq.platform.core.logging import JsonFormatter, reset_request_id, set_request_id
from hq.platform.core.middleware import CurrentUserMiddleware, get_current_user, set_current_user
from hq.platform.core.models import AuditLog
from hq.platform.core.oidc import TAILSCALE_PRINCIPAL_SESSION_KEY, HQOIDCAuthenticationBackend

User = get_user_model()


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

    def test_inline_styles_are_the_only_relaxation_in_the_policy(self):
        """Pin the one exception so it stays one exception.

        ``style-src`` allows ``'unsafe-inline'`` because charts position their
        marks with a per-datum custom property (``style="--at: 62%"``) which
        no class can express and no nonce can cover, because nonces apply to
        style *elements* and not to style *attributes*.

        That is a deliberate, bounded exception. It is asserted here rather
        than merely documented, because the failure mode of a documented rule
        is that a second directive quietly joins the first and the policy is
        weaker than the prose claims. Anything relaxed beyond this fails.
        """

        response = self.client.get("/accounts/login/")
        policy = response.headers["Content-Security-Policy"]
        directives = {
            directive.split()[0]: directive.split()[1:] for directive in policy.split(";") if directive.strip()
        }

        self.assertEqual(directives["default-src"], ["'self'"])
        self.assertEqual(directives["script-src"], ["'self'"])
        self.assertEqual(directives["object-src"], ["'none'"])
        self.assertEqual(directives["frame-ancestors"], ["'none'"])
        relaxed = sorted(
            directive.split()[0]
            for directive in policy.split(";")
            if "'unsafe-inline'" in directive or "'unsafe-eval'" in directive
        )
        self.assertEqual(relaxed, ["style-src"])

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
    def test_current_user_context_never_retains_session_lazy_object(self):
        request = self.client.request().wsgi_request
        SessionMiddleware(lambda value: value).process_request(request)
        AuthenticationMiddleware(lambda value: value).process_request(request)
        observed = CurrentUserMiddleware(lambda _request: get_current_user())(request)

        self.assertIs(observed, request._cached_user)
        self.assertNotEqual(type(observed).__name__, "SimpleLazyObject")

    def test_json_logs_carry_request_correlation_without_query_strings(self):
        token = set_request_id("request-123")
        try:
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
        finally:
            reset_request_id(token)

        self.assertEqual(payload["request_id"], "request-123")
        self.assertEqual(payload["path"], "/search/")
        self.assertEqual(payload["duration_ms"], 4.2)

    def test_request_user_attribution_is_isolated_between_async_tasks(self):
        async def exercise():
            ready = [asyncio.Event(), asyncio.Event()]
            release = asyncio.Event()

            async def worker(index, user):
                set_current_user(user)
                ready[index].set()
                await release.wait()
                try:
                    return get_current_user()
                finally:
                    set_current_user(None)

            tasks = [
                asyncio.create_task(worker(0, "first-user")),
                asyncio.create_task(worker(1, "second-user")),
            ]
            await asyncio.gather(*(event.wait() for event in ready))
            release.set()
            return await asyncio.gather(*tasks)

        self.assertEqual(async_to_sync(exercise)(), ["first-user", "second-user"])
        self.assertIsNone(get_current_user())


@tag("source")
class TemplateSourceTests(SimpleTestCase):
    def test_application_templates_do_not_bypass_csp(self):
        violations = []
        for template in (settings.BASE_DIR / "templates").rglob("*.html"):
            source = template.read_text(encoding="utf-8")
            if re.search(r"<script(?![^>]*\bsrc=)", source, re.IGNORECASE):
                violations.append(f"{template}: inline script")
            if re.search(r"\son[a-z]+\s*=", source, re.IGNORECASE):
                violations.append(f"{template}: inline event handler")

        self.assertEqual(violations, [])


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
            SEVERINO_OIDC_ALLOWED_EMAILS=set(),
            SEVERINO_OIDC_ALLOWED_GROUPS={"admins"},
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

    def test_matches_existing_user_by_preferred_username_without_email(self):
        user = User.objects.create_user(username="joe")
        backend = HQOIDCAuthenticationBackend()

        users = backend.filter_users_by_claims({"sub": "subject-1", "preferred_username": "joe", "groups": ["admins"]})

        self.assertEqual(list(users), [user])

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
            list(backend.filter_users_by_claims({"sub": "subject-2", "preferred_username": "sam"})), [other]
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

        with override_settings(SEVERINO_OIDC_ALLOWED_GROUPS={"admins"}):
            self.assertFalse(backend.verify_claims({"preferred_username": "joe", "groups": ["admins"]}))

    def test_allows_user_by_allowed_email(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS={"joe@example.com"},
            SEVERINO_OIDC_ALLOWED_GROUPS=set(),
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

        with (
            override_settings(
                SEVERINO_OIDC_ALLOWED_EMAILS=set(),
                SEVERINO_OIDC_ALLOWED_GROUPS=set(),
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


class _AuthedTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_superuser(
            username="tester",
            email="t@example.com",
            password="strongtestpass-1234",
        )

    def setUp(self):
        self.client = Client()
        assert self.client.login(username="tester", password="strongtestpass-1234")


class ManifestUploadTests(_AuthedTestCase):
    def upload(self, content):
        from django.core.files.uploadedfile import SimpleUploadedFile

        return self.client.post(
            "/docs/import/",
            {"manifest_file": SimpleUploadedFile("manifest.json", content, "application/json")},
        )

    def test_a_file_that_is_not_utf8_is_refused_with_a_reason(self):
        response = self.upload(b"\xff\xfe[]")

        self.assertEqual(response.status_code, 200)
        self.assertIn("not UTF-8", text(response))

    def test_an_oversized_manifest_is_refused_before_it_is_read(self):
        from hq.domains.docs_index.forms import MAX_MANIFEST_BYTES

        with patch("hq.domains.docs_index.forms.MAX_MANIFEST_BYTES", 8):
            response = self.upload(b"[" + b" " * 16 + b"]")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "A manifest is at most")
        self.assertGreater(MAX_MANIFEST_BYTES, 8)


class SearchPageTests(_AuthedTestCase):
    def test_search_with_nothing_asked_is_a_search_box(self):
        with patch("hq.platform.core.views.command_center") as discovery:
            response = self.client.get("/search/")

        discovery.assert_not_called()
        self.assertTrue(select(response, "name", "q"))
        self.assertFalse([url for url in hrefs(response) if url.startswith(command_url("project.create")[:10])])

    def test_a_query_finds_pages_and_commands_by_their_names_and_shows_no_identifier(self):
        response = self.client.get("/search/", {"q": "project"})

        links = hrefs(response)
        self.assertIn(command_url("project.create"), links)
        self.assertIn(reverse("projects:list"), links)
        self.assertNotIn("project.create", text(response))

    def test_query_filters_commands_as_well_as_records(self):
        response = self.client.get("/search/", {"q": "certificate.renew"})

        links = hrefs(response)
        self.assertIn(command_url("certificate.renew"), links)
        self.assertNotIn(command_url("project.create"), links)

    def test_palette_is_a_small_fragment_with_local_record_search(self):
        with (
            patch(
                "hq.platform.core.views.global_search",
                return_value={"groups": [], "total": 0},
            ) as records,
            patch("hq.domains.contacts.inbox.search") as contacts,
        ):
            response = self.client.get(
                "/search/",
                {"q": "certificate"},
                HTTP_X_FRAGMENT="palette",
            )

        self.assertTemplateUsed(response, "core/_command_center_results.html")
        self.assertTrue(select(response, "data-command-center-results"))
        self.assertFalse(elements(response, "html"))
        records.assert_called_once()
        self.assertEqual(records.call_args.args, ("certificate",))
        self.assertEqual(records.call_args.kwargs["limit_per_scope"], 3)
        contacts.assert_not_called()

    def test_palette_includes_indexed_records_beside_destinations(self):
        project = Project.objects.create(
            name="Palette record target",
            description="A uniquely indexed command center result.",
        )

        response = self.client.get(
            "/search/",
            {"q": "Palette record target"},
            HTTP_X_FRAGMENT="palette",
        )

        self.assertIn(project.get_absolute_url(), hrefs(response))

    def test_palette_searches_content_not_only_navigation(self):
        item = ContentItem.objects.create(
            title="Palette content target",
            notes="A uniquely indexed piece of content.",
        )

        response = self.client.get(
            "/search/",
            {"q": "Palette content target"},
            HTTP_X_FRAGMENT="palette",
        )

        self.assertIn(item.get_absolute_url(), hrefs(response))

    def test_authenticated_pages_ship_the_global_palette_progressively(self):
        response = self.client.get("/projects/")

        self.assertTrue(select(response, "id", "modal-command-center"))
        (form,) = select(response, "data-command-center-form")
        self.assertEqual((form.tag, form.attrs["action"]), ("form", reverse("search")))
        opener = select(response, "data-command-center-open")
        self.assertTrue(opener)
        self.assertTrue(all(each.attrs.get("aria-controls") == "modal-command-center" for each in opener))

    def test_results_highlight_matches_and_skip_empty_scopes(self):

        Project.objects.create(
            name="Highlight target",
            description="global search rendering check",
        )
        response = self.client.get("/search/", {"q": "highlight"})

        self.assertIn("Highlight", " ".join(mark.text for mark in elements(response, "mark")))
        # Empty scopes are omitted entirely, not rendered as empty cards.
        shown = {each.attrs["id"] for each in select(response, "id") if each.attrs["id"].startswith("scope-")}
        self.assertEqual(shown, {f"scope-{group['scope']}" for group in response.context["groups"] if group["count"]})


class EmptyListTests(_AuthedTestCase):
    def test_an_empty_list_is_the_empty_state_and_no_table(self):
        """Headings over no rows say nothing; the one empty state says what is missing."""

        page = self.client.get(reverse("expenses:list"))

        self.assertTrue(select(page, "data-empty-state"))
        self.assertFalse(elements(page, "table"))

    def test_a_list_emptied_by_its_filter_offers_the_way_back(self):
        page = self.client.get(reverse("expenses:list"), {"q": "nothing-matches-this"})

        self.assertTrue(select(page, "data-empty-state"))
        self.assertIn(page.context["table"]["clear"].url, hrefs(page))
        self.assertFalse(elements(page, "table"))


class KeyColumnTests(_AuthedTestCase):
    """A list says which columns a phone keeps, on the heading, sortable or not."""

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        # An empty list draws no table, so its headings need rows under them.
        from hq.platform.core import example_estate

        with example_estate.throwaway_media():
            example_estate.seed()

    def test_a_sortable_heading_carries_its_columns_classes(self):
        page = self.client.get(reverse("expenses:list"))

        headings = elements(page, "th")

        def heading(label):
            return next(each.attrs for each in headings if each.text.strip().startswith(label))

        # Cost sorts, and is one of the two an expense is read for.
        self.assertIn("aria-sort", heading("Cost"))
        self.assertIn("key-col", heading("Cost").get("class", "").split())
        # A column that is not key is not kept on a phone.
        self.assertIn("aria-sort", heading("Category"))
        self.assertNotIn("key-col", heading("Category").get("class", "").split())

    def test_every_shared_list_names_at_least_one_key_column(self):
        """With none, a phone would show only the column that names the row."""

        for name in ("expenses:list", "projects:list", "content:list", "assets:list", "core:audit_list"):
            with self.subTest(page=name):
                headings = elements(self.client.get(reverse(name)), "th")
                self.assertTrue(any("key-col" in heading.attrs.get("class", "").split() for heading in headings))


class DashboardWorkflowTests(_AuthedTestCase):
    def test_dashboard_and_machine_page_share_the_machine_observation(self):
        observed = timezone.now()
        machine = ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={"name": "example-host"},
        )
        readings.record(
            readings.machine_telemetry("example-host"),
            {
                "status": "good",
                "summary": "Host load 0.30",
                "metrics": [{"label": "CPU", "value": "9%", "detail": "8 cores"}],
            },
            observed_at=observed,
        )
        DashboardMachine.objects.create(machine=machine)

        dashboard = self.client.get("/")
        machine_page = self.client.get("/infrastructure/machines/example-host/")

        self.assertIn("9%", text(dashboard))
        self.assertIn("9%", text(machine_page))

    def test_dashboard_refresh_is_one_explicit_controller_request(self):
        machine = ManagedResource.objects.create(
            key="example-host",
            kind="machine",
            spec={"name": "example-host"},
        )
        DashboardMachine.objects.create(machine=machine)
        ProviderConnection.objects.create(
            connection_ref="example-portainer",
            controller_id="example-host",
            provider="portainer",
            endpoint="https://portainer.example.test",
            reaches=["example-host"],
            reachable=True,
            observed_at=timezone.now(),
        )

        with (
            self.captureOnCommitCallbacks(execute=True),
            patch("hq.platform.application.glance.ring_doorbell") as ring,
        ):
            response = self.client.post("/dashboard/glance/")

        self.assertEqual(response.status_code, 202)
        self.assertTrue(select(response, "data-refreshing"))
        self.assertEqual(
            list(DashboardRefreshRequest.objects.values_list("panel_id", flat=True)),
            [f"machine-{machine.pk}"],
        )
        ring.assert_called_once_with()

    def test_dashboard_weather_settings_are_managed_in_the_ui(self):
        response = self.client.post(
            "/dashboard/glance/settings/",
            {
                "infrastructure_label": "Server room",
                "weather_point": "41.1, -87.2",
                "weather_label": "Outside",
            },
        )

        self.assertRedirects(response, "/")
        configuration = DashboardConfiguration.objects.get()
        self.assertEqual(configuration.infrastructure_label, "Server room")
        self.assertEqual(configuration.weather_point, "41.1000,-87.2000")
        self.assertEqual(configuration.weather_label, "Outside")

    def test_action_items_is_the_full_existing_queue_and_filters_it(self):
        ContentItem.objects.create(title="Unfinished post", status=ContentItem.Status.DRAFT)

        response = self.client.get("/action-items/")
        filtered = self.client.get("/action-items/", {"status": "serious", "q": "draft"})

        drafts = f"{reverse('content:list')}?status=draft"
        self.assertIn(drafts, hrefs(response))
        # Filtered to urgent only, so an entry that is not urgent is gone even
        # though the query still matches its text.
        self.assertNotIn(drafts, hrefs(filtered))

    def test_profile_count_is_lazy_off_dashboard(self):
        ContentItem.objects.create(title="Count me", status=ContentItem.Status.DRAFT)

        page = self.client.get("/projects/")
        with host_only():
            count = self.client.get("/action-items/count/")

        (badge,) = select(page, "data-action-count")
        self.assertIn("hidden", badge.attrs)
        self.assertIn("data-fragment-load", badge.attrs)
        self.assertEqual(badge.attrs["data-fragment"], "/action-items/count/")
        # The answer is the same region, counted, and asks for nothing more.
        (answer,) = select(count, "data-action-count")
        self.assertEqual(answer.attrs["id"], badge.attrs["id"])
        self.assertNotIn("data-fragment", answer.attrs)
        self.assertNotIn("hidden", answer.attrs)
        self.assertEqual(count.context["profile_action_count"], 1)

    def test_the_controllers_dot_says_only_what_is_known(self):
        """Green while it is arriving, the attention color once it has gone
        quiet, and neither where it has never run."""

        marker = Path(tempfile.mkdtemp()) / "controller-heartbeat"
        with self.settings(SEVERINO_CONTROLLER_HEARTBEAT=str(marker)):
            never = self.client.get("/")
            marker.touch()
            arriving = self.client.get("/")
            long_ago = time.time() - 3600
            os.utime(marker, (long_ago, long_ago))
            silent = self.client.get("/")

        states = {
            name: (page.context["controller"].known, page.context["controller"].silent)
            for name, page in (("never", never), ("arriving", arriving), ("silent", silent))
        }
        self.assertEqual(states, {"never": (False, False), "arriving": (True, False), "silent": (True, True)})
        self.assertIn(reverse("control_plane:controller"), hrefs(silent))

    def test_the_dashboard_draws_its_contacts_without_waiting_on_d1(self):
        rows = [
            {"id": 7, "name": "One call", "status": "unread", "created_at": "2026-08-23", "database_id": "example"},
            *({"id": number, "status": "unread", "database_id": "example"} for number in (6, 5)),
        ]
        ProviderInventory.objects.create(kind="cloudflare.contact_submission", records=rows, observed_at=timezone.now())
        page = self.client.get("/")

        # With the page, from the stored rows: nothing is fetched afterwards,
        # so no card appears and then changes.
        self.assertEqual(page.context["unread_contacts_count"], 3)
        self.assertIn("One call", text(page))
        # Inside the page itself; the connection dialog every page carries
        # reads only when it is opened.
        main = page.content.decode().split("<main", 1)[1].split("</main>", 1)[0]
        self.assertNotIn("data-fragment-load", main)

    def test_an_empty_contact_feed_draws_no_card(self):
        page = self.client.get("/")

        self.assertFalse(page.context["recent_contacts"])

    def test_the_dashboard_says_how_this_request_reached_it(self):
        """The reading the connection panel opens on, and a link to that panel."""

        page = self.client.get("/")
        panel = self.client.get(reverse("connection"))

        found = page.context["request_connection"]
        self.assertEqual(found.summary, panel.context["connection"].summary)
        # It opens the panel, and is a plain link to it without script.
        badge = next(each for each in select(page, "data-modal-open", "connection") if found.summary in each.text)
        self.assertEqual(badge.attrs["href"], reverse("connection"))
        self.assertIn(found.transport, badge.text)

    def test_dashboard_routes_infrastructure_findings_to_their_evidence(self):
        ManagedResource.objects.create(
            key="example-wildcard",
            kind="tls.certificate",
            spec={},
            generation=2,
            observed_generation=1,
            conditions=[
                {
                    "type": "Degraded",
                    "status": True,
                    "reason": "ExpiringSoon",
                    "message": "Certificate expires tomorrow.",
                }
            ],
        )

        response = self.client.get("/action-items/")

        # Each finding is its own item, linked to its own rule's evidence.
        rule_links = f"{reverse('control_plane:findings')}?rule="
        self.assertTrue([url for url in hrefs(response) if url.startswith(rule_links)])

    def test_output_a_project_lacks_is_counted_but_not_queued(self):
        """A shape of the portfolio, not a decision waiting on anyone.

        Nothing clears "active work with nothing written about it yet" except
        months of work, so as a queue entry it would be permanently the largest and
        permanently unactionable. The count still shows on the projects card.
        """

        Project.objects.create(name="Documentable lab", status=Project.Status.ACTIVE)

        response = self.client.get("/")

        self.assertIn("1 has no writeup or doc", text(response))
        queue = self.client.get(reverse("action_items"))
        self.assertNotIn(f"{reverse('projects:list')}?needs_output=1", hrefs(queue))

    def test_dashboard_excludes_site_pages_from_docs_review(self):
        DocumentationRecord.objects.create(
            doc_id="page-about",
            title="About page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
        )

        self.assertEqual(operating_snapshot(principal=cli_principal())["kpis"]["docs_needing_review"], 0)

    def test_projects_can_filter_for_missing_output(self):
        project = Project.objects.create(name="No output yet", status=Project.Status.ACTIVE)
        documented = Project.objects.create(name="Documented", status=Project.Status.ACTIVE)
        doc = DocumentationRecord.objects.create(
            doc_id="rb-documented-001",
            title="Documented runbook",
            status=DocumentationRecord.Status.ACTIVE,
        )
        doc.related_projects.add(documented)
        content = ContentItem.objects.create(title="Documented post")
        content.related_projects.add(documented)

        response = self.client.get("/projects/?needs_output=1")

        self.assertEqual(list(response.context["object_list"]), [project])

    def test_archived_projects_sort_last_by_default(self):
        active = Project.objects.create(name="ZZZ active project", status=Project.Status.ACTIVE)
        archived = Project.objects.create(name="AAA archived project", status=Project.Status.ARCHIVED)

        response = self.client.get("/projects/")

        listed = list(response.context["object_list"])
        self.assertLess(listed.index(active), listed.index(archived))

    def test_table_filters_accept_multiple_values_and_compose(self):
        active_lab = Project.objects.create(
            name="Active lab",
            status=Project.Status.ACTIVE,
            category="homelab",
        )
        idea_lab = Project.objects.create(
            name="Idea lab",
            status=Project.Status.IDEA,
            category="homelab",
        )
        Project.objects.create(
            name="Archived lab",
            status=Project.Status.ARCHIVED,
            category="homelab",
        )
        Project.objects.create(
            name="Active consulting",
            status=Project.Status.ACTIVE,
            category="other",
        )

        response = self.client.get(
            "/projects/",
            {"status": ["active", "idea"], "category": ["homelab"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertCountEqual(response.context["object_list"], [active_lab, idea_lab])
        status_filter = next(item for item in response.context["table"]["filters"] if item["name"] == "status")
        self.assertEqual(status_filter["selected_count"], 2)

    def test_table_sort_is_allowlisted(self):
        Project.objects.create(name="Safe sort")

        response = self.client.get("/projects/", {"sort": "not_a_model_field"})

        self.assertEqual(response.status_code, 200)

    def test_a_search_that_matches_nothing_is_an_empty_table_not_an_error(self):
        """A search matching nothing still carries `_search_rank`.

        The table layer orders by it, and Django resolves an ordering name
        against the model whether or not a row is built, so a bare `.none()`
        would answer 500 on every list page to a misspelt vendor."""

        Project.objects.create(name="Something else entirely")

        for path in ("/projects/", "/expenses/", "/docs/", "/audit/", "/receipts/"):
            with self.subTest(path=path):
                response = self.client.get(path, {"q": "zzzznomatchhere"})
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "Something else entirely")

    def test_table_search_combines_terms_across_searchable_fields(self):
        matching = Project.objects.create(
            name="Operations console",
            technologies_used="Django SQLite",
        )
        Project.objects.create(name="Operations notes", technologies_used="Markdown")

        response = self.client.get("/projects/", {"q": "operations django"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["object_list"]), [matching])

    @override_settings(SEVERINO_DOC_REVIEW_INTERVAL_DAYS=30)
    def test_docs_review_filter_uses_configured_interval(self):
        current = DocumentationRecord.objects.create(
            doc_id="rb-current-001",
            title="Current runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=20),
        )
        stale = DocumentationRecord.objects.create(
            doc_id="rb-stale-001",
            title="Stale runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )
        site_page = DocumentationRecord.objects.create(
            doc_id="page-stale-001",
            title="Stale site page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )

        response = self.client.get("/docs/?needs_review=1")

        listed = list(response.context["object_list"])
        self.assertIn(stale, listed)
        self.assertNotIn(current, listed)
        self.assertNotIn(site_page, listed)

    @override_settings(SEVERINO_DOC_REVIEW_INTERVAL_DAYS=30)
    def test_reports_docs_review_uses_shared_definition(self):
        stale = DocumentationRecord.objects.create(
            doc_id="rb-stale-002",
            title="Stale runbook",
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )
        DocumentationRecord.objects.create(
            doc_id="page-stale-002",
            title="Stale site page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
            last_reviewed=timezone.localdate() - timedelta(days=31),
        )

        response = self.client.get("/reports/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["docs_needing_review_count"], 1)
        self.assertEqual(
            [link.title for link, _reviewed in response.context["docs_needing_review"]],
            [stale.doc_id],
        )


class ExportSmokeTests(_AuthedTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(name="Lab: homelab DNS", status=Project.Status.ACTIVE)
        self.asset = Asset.objects.create(
            item_name="Test switch",
            total_cost=Decimal("29.00"),
            business_use_percentage=100,
            purchase_date=date.today(),
            status=Asset.Status.ACTIVE,
        )
        self.expense = Expense.objects.create(
            date=date.today(),
            vendor="Cloudflare",
            item="Cloudflare Pro",
            category="hosting",
            total_cost=Decimal("240.00"),
            business_use_percentage=100,
            related_project=self.project,
        )
        self.doc = DocumentationRecord.objects.create(
            doc_id="rb-test-001",
            title="Test runbook",
            obsidian_path="Infra/Test.md",
        )
        self.content = ContentItem.objects.create(title="Test article", status=ContentItem.Status.DRAFT)

    def test_csv_exports(self):
        for url in [
            "/reports/export/expenses.csv",
            "/reports/export/assets.csv",
            "/reports/export/content.csv",
            "/reports/export/projects.csv",
            "/reports/export/documentation.csv",
        ]:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200, url)
                self.assertEqual(r["Content-Type"].split(";")[0], "text/csv")

    def test_year_summary_json_has_relationships(self):
        r = self.client.get("/reports/export/year-summary.json")
        self.assertEqual(r.status_code, 200)
        data = json.loads(r.content.decode("utf-8"))
        # Stable slugs / doc_ids exposed for the future MCP.
        slugs = {p["slug"] for p in data["projects"]}
        self.assertIn(self.project.slug, slugs)
        doc_ids = {d["doc_id"] for d in data["documentation"]}
        self.assertIn(self.doc.doc_id, doc_ids)
        # Disclaimer present and deductible math reflected.
        self.assertIn("not tax advice", data["disclaimer"].lower())

    def test_year_summary_markdown_is_ai_readable(self):
        r = self.client.get("/reports/export/year-summary.md")
        self.assertEqual(r.status_code, 200)
        body = r.content.decode("utf-8")
        self.assertIn("# Severino HQ year summary", body)
        self.assertIn("Cloudflare", body)
        self.assertIn("Test runbook", body)


class ManifestImportTests(TestCase):
    def test_manifest_backfills_blank_project_technologies_from_tags(self):
        project = Project.objects.create(
            name="Techless project",
            slug="techless-project",
            status=Project.Status.ACTIVE,
        )

        stats = import_manifest_data(
            [
                {
                    "doc_id": "project-techless-project",
                    "title": "Techless project",
                    "doc_type": DocumentationRecord.DocType.ARCHITECTURE_NOTE,
                    "status": DocumentationRecord.Status.ACTIVE,
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                    "related_projects": [project.slug],
                    "tags": ["django", "sqlite", "tailscale"],
                }
            ]
        )

        project.refresh_from_db()
        self.assertEqual(project.technologies_used, "django, sqlite, tailscale")
        self.assertEqual(stats["projects_tech_backfilled"], 1)

    def test_manifest_does_not_overwrite_curated_project_technologies(self):
        project = Project.objects.create(
            name="Curated project",
            slug="curated-project",
            status=Project.Status.ACTIVE,
            technologies_used="Django, PostgreSQL",
        )

        stats = import_manifest_data(
            [
                {
                    "doc_id": "project-curated-project",
                    "title": "Curated project",
                    "doc_type": DocumentationRecord.DocType.ARCHITECTURE_NOTE,
                    "status": DocumentationRecord.Status.ACTIVE,
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                    "related_projects": [project.slug],
                    "tags": ["sqlite", "tailscale"],
                }
            ]
        )

        project.refresh_from_db()
        self.assertEqual(project.technologies_used, "Django, PostgreSQL")
        self.assertNotIn("projects_tech_backfilled", stats)

    def test_a_task_note_is_not_a_doc_the_vault_writes(self):
        # The vault keeps no task notes: their type and lifecycle are refused.
        for case, entry in (
            ("task type", {**self.STANDARD_DOC, "doc_id": "rb-x", "doc_type": "task"}),
            ("task status", {**self.STANDARD_DOC, "doc_id": "rb-x", "status": "parked"}),
        ):
            with self.subTest(case), self.assertRaises(ManifestImportError):
                import_manifest_data([entry])
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    def test_validate_manifest_data_flags_bad_enums_without_db(self):
        # The read-only preflight catches the contract-drift class (an invalid
        # status/doc_type/environment) before it reaches the deployed importer,
        # and writes nothing.
        problems = validate_manifest_data(
            [
                {
                    "doc_id": "rb-ok",
                    "title": "ok",
                    "doc_type": "runbook",
                    "status": "active",
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                },
                {
                    "doc_id": "rb-bad-status",
                    "title": "bad",
                    "doc_type": "runbook",
                    "status": "open",
                    "system": "Example service",
                    "environment": "other",
                    "sensitivity": "internal",
                },
                {"doc_id": "rb-bad-env", "doc_type": "runbook", "environment": "nope"},
            ]
        )
        by = {p["doc_id"]: p for p in problems}
        self.assertNotIn("rb-ok", by)
        self.assertIn("rb-bad-status", by)
        self.assertIn("rb-bad-env", by)
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    # An entry breaking the schema's prefixes or required fields is refused,
    # not upserted. Each rejection is asserted on
    # both paths (the preflight and the write) since they share one check.
    STANDARD_DOC = {
        "doc_id": "rb-complete",
        "title": "Complete",
        "doc_type": "runbook",
        "system": "Example service",
        "environment": "other",
        "status": "active",
        "sensitivity": "internal",
    }

    def _assert_rejected(self, entry, *fragments):
        problems = validate_manifest_data([entry])
        self.assertEqual(len(problems), 1, problems)
        self.assertEqual(problems[0]["doc_id"], entry["doc_id"])
        message = problems[0]["errors"][0]
        for fragment in (entry["doc_id"], *fragments):
            self.assertIn(fragment, message)
        with self.assertRaisesRegex(ManifestImportError, entry["doc_id"]):
            import_manifest_data([entry])
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    CONTENT_DOC = {
        **STANDARD_DOC,
        "doc_id": "writeup-complete",
        "doc_type": "public_article_draft",
        "content_type": "portfolio_article",
        "slug": "complete",
        "published": False,
    }

    def test_an_entry_off_the_schema_is_refused_by_the_preflight_and_the_write(self):
        def without(entry, *fields):
            return {key: value for key, value in entry.items() if key not in fields}

        for case, entry, names in (
            ("prefix", {**self.STANDARD_DOC, "doc_id": "wiki-complete"}, ("rb-",)),
            ("missing", without(self.STANDARD_DOC, "environment", "sensitivity"), ("environment", "sensitivity")),
            ("blank", {**self.STANDARD_DOC, "system": ""}, ("system",)),
            # The schema's prefixes bind every vault doc: a runbook cannot pass with a writeup's id.
            ("vault doc with a content id", {**self.STANDARD_DOC, "doc_id": "writeup-complete"}, ()),
            # Not routed to ContentItem, so held to the schema's prefixes.
            ("article without a content type", without(self.CONTENT_DOC, "content_type"), ()),
            ("content missing", without(self.CONTENT_DOC, "system"), ("system",)),
            ("content without its item", without(self.CONTENT_DOC, "slug", "published"), ("slug", "published")),
            ("content with a blank slug", {**self.CONTENT_DOC, "slug": ""}, ("slug",)),
        ):
            with self.subTest(case):
                self._assert_rejected(entry, *names)

    def test_content_entry_id_is_not_held_to_the_doc_prefixes(self):
        for doc_id in ("writeup-complete", "page-about", "anything-at-all"):
            with self.subTest(doc_id=doc_id):
                entry = {**self.CONTENT_DOC, "doc_id": doc_id}
                self.assertEqual(validate_manifest_data([entry]), [])

    def test_valid_standard_doc_passes(self):
        manifest = [self.STANDARD_DOC]
        self.assertEqual(validate_manifest_data(manifest), [])
        stats = import_manifest_data(manifest)
        self.assertEqual(stats["created"], 1)

    def test_check_only_command_gates_an_invalid_manifest(self):
        bad = json.dumps([{**self.STANDARD_DOC, "status": "open"}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(bad)
            bad_path = f.name
        ok = json.dumps([self.STANDARD_DOC])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(ok)
            ok_path = f.name
        try:
            with self.assertRaises(CommandError):
                call_command("import_docs_manifest", bad_path, "--check-only", stdout=StringIO())
            call_command("import_docs_manifest", ok_path, "--check-only", stdout=StringIO())  # no raise
            self.assertEqual(DocumentationRecord.objects.count(), 0)  # read-only either way
        finally:
            Path(bad_path).unlink()
            Path(ok_path).unlink()

    def test_public_article_content_item_uses_manifest_slug(self):
        import_manifest_data(
            [
                {
                    "doc_id": "writeup-example-article",
                    "slug": "writing-an-example-article",
                    "title": "Writing an Example Article",
                    "doc_type": DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
                    "system": "example.com",
                    "environment": DocumentationRecord.Environment.CLOUDFLARE,
                    "status": DocumentationRecord.Status.DRAFT,
                    "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                    "content_type": "portfolio_article",
                    "published": False,
                    "path": "example/writing-an-example-article/index.md",
                }
            ]
        )

        record = DocumentationRecord.objects.get(doc_id="writeup-example-article")
        item = ContentItem.objects.get(slug="writing-an-example-article")

        self.assertTrue(item.related_documentation.filter(pk=record.pk).exists())
        self.assertFalse(ContentItem.objects.filter(slug="example-article").exists())

    def test_prune_removes_content_item_only_linked_to_orphan_doc(self):
        old_record = DocumentationRecord.objects.create(
            doc_id="writeup-old-slug",
            title="Old title",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
        )
        stale_item = ContentItem.objects.create(
            slug="old-slug",
            title="Old title",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
        )
        stale_item.related_documentation.add(old_record)

        stats = import_manifest_data(
            [],
            report_orphans=True,
            prune_orphans=True,
        )

        self.assertEqual(stats["orphans_pruned"], 1)
        self.assertEqual(stats["content_items_pruned"], 1)
        self.assertFalse(ContentItem.objects.filter(slug="old-slug").exists())


class AuditLogTests(TestCase):
    def test_create_writes_audit_event(self):
        before = AuditLog.objects.count()
        Project.objects.create(name="audited", status=Project.Status.IDEA)
        # Audit signals attribute to no user when called outside a request,
        # but the event itself should be written.
        self.assertEqual(AuditLog.objects.count(), before + 1)
        event = AuditLog.objects.first()
        self.assertEqual(event.object_type, "Project")
        self.assertEqual(event.action, AuditLog.Action.CREATED)


class AuditChangeTests(TestCase):
    """An update has to say what changed, or it is only saying that it did."""

    def test_an_update_records_which_fields_moved_from_the_last_save_and_to_what(self):
        project = Project.objects.create(name="Before", status=Project.Status.IDEA)
        project.name = "After"
        project.status = Project.Status.ACTIVE
        project.save()
        project.name = "Again"
        project.save()

        first, second = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).order_by("pk")
        self.assertEqual(first.metadata["changes"], {"name": ["Before", "After"], "status": ["idea", "active"]})
        self.assertEqual(second.metadata["changes"], {"name": ["After", "Again"]})

    def test_a_save_that_changed_nothing_is_not_an_event(self):
        # Django writes every column on every save; an unchanged re-submission
        # records no event.
        project = Project.objects.create(name="Steady", status=Project.Status.IDEA)
        before = AuditLog.objects.count()

        project.save()

        self.assertEqual(AuditLog.objects.count(), before)

    def test_values_that_are_not_json_survive_the_round_trip(self):
        # A Decimal, a date and a UUID are none of the things JSON has. Left
        # as they are the row fails to write, `record_event` swallows it, and
        # the change is lost with nothing saying so.
        expense = Expense.objects.create(date=date.today(), vendor="V", item="I", total_cost=Decimal("1.00"))
        expense.total_cost = Decimal("2.50")
        expense.save()

        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED, object_type="Expense").first()
        self.assertIsNotNone(event, "the row must be written, not swallowed")
        self.assertEqual(event.metadata["changes"]["total_cost"], ["1.00", "2.50"])

    def test_a_redacted_field_reports_the_change_without_the_value(self):
        from hq.platform.core.audit import REDACTED, _changes

        changes = _changes({"token": "old-secret"}, {"token": "new-secret"}, frozenset({"token"}))

        # That it rotated is worth recording. What it rotated to is not.
        self.assertEqual(changes["token"], [REDACTED, REDACTED])


class AuditDetailPageTests(_AuthedTestCase):
    """Every row leads somewhere, and what it leads to answers the question."""

    def test_an_event_shows_the_fields_that_moved(self):
        project = Project.objects.create(name="Before", status=Project.Status.IDEA)
        project.name = "After"
        project.save()
        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).first()

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        shown = text(page)
        self.assertIn("Before", shown)
        self.assertIn("After", shown)
        self.assertEqual(page.context["page"].trail, (("Audit log", reverse("core:audit_list")),))
        # What else happened to the same object: its creation.
        created = AuditLog.objects.get(object_id=str(project.pk), action=AuditLog.Action.CREATED)
        self.assertIn(reverse("core:audit_detail", args=[created.pk]), hrefs(page))

    def test_a_record_field_that_moved_is_said_in_words(self):
        from hq.domains.control_plane.models import ManagedResource

        resource = ManagedResource.objects.create(
            key="example-policy",
            kind="example.policy",
            spec={"grants": [{"src": ["group:example"], "dst": ["tag:example"]}], "hosts": {"one": "192.0.2.1"}},
        )
        resource = ManagedResource.objects.get(pk=resource.pk)
        resource.spec["grants"][0]["src"].append("group:elsewhere")
        resource.spec["hosts"]["one"] = "192.0.2.2"
        with operation_context(interface="cli", actor="op", operation="example.edit"):
            resource.save()
        event = AuditLog.objects.get(action=AuditLog.Action.UPDATED, object_id=str(resource.pk))

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))
        shown = text(page)

        self.assertIn("Spec › Grants › Entry 1 › Src", shown)
        self.assertIn("group:example, group:elsewhere", shown)
        self.assertIn("Spec › Hosts › One", shown)
        self.assertIn("192.0.2.2", shown)
        self.assertNotIn("{'", shown)
        self.assertNotIn("['", shown)
        # What was recorded beside the change is named in words too.
        self.assertEqual(set(page.context["extra"]), {"Actor", "Interface", "Operation"})

    def test_an_event_links_to_the_rest_of_its_operation(self):
        # The point of operation_id: one action touching several rows is one
        # thing that happened, not four events that share a timestamp.
        with operation_context(interface="cli", actor="op", operation="test.bulk", operation_id="op-1"):
            Project.objects.create(name="One", status=Project.Status.IDEA)
            Project.objects.create(name="Two", status=Project.Status.IDEA)
        event, other = AuditLog.objects.filter(operation_id="op-1")

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        self.assertIn(reverse("core:audit_detail", args=[other.pk]), hrefs(page))

    def test_the_operation_and_the_objects_history_are_one_table_whose_history_folds(self):
        with operation_context(interface="cli", actor="op", operation="test.bulk", operation_id="op-1"):
            project = Project.objects.create(name="One", status=Project.Status.IDEA)
            Project.objects.create(name="Two", status=Project.Status.IDEA)
        project.name = "Renamed"
        project.save()
        created = AuditLog.objects.get(object_id=str(project.pk), action=AuditLog.Action.CREATED)
        updated = AuditLog.objects.get(object_id=str(project.pk), action=AuditLog.Action.UPDATED)

        page = self.client.get(reverse("core:audit_detail", args=[created.pk]))

        inside = within(page, "aria-label", "Other events")
        self.assertEqual(len([each for each in inside if each.tag == "table"]), 1)
        self.assertEqual(len([each for each in inside if each.attrs.get("scope") == "rowgroup"]), 2)
        (group,) = [each for each in inside if "data-folds" in each.attrs]
        (folded,) = [each for each in inside if "data-folded" in each.attrs]
        self.assertEqual(folded.attrs["data-folded"], group.attrs["id"])
        # The object's own history is what folds; the rest of the operation stays open.
        (link,) = [each for each in inside if each.attrs.get("href") == reverse("core:audit_detail", args=[updated.pk])]
        self.assertTrue(any(ancestor is folded for ancestor in link.ancestors))

    def test_the_detail_page_needs_a_session(self):
        project = Project.objects.create(name="Private", status=Project.Status.IDEA)
        event = AuditLog.objects.filter(object_id=str(project.pk)).first()
        self.client.logout()

        response = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        self.assertEqual(response.status_code, 302)


class ReceiptFileProtectionTests(_AuthedTestCase):
    def _make_receipt(self) -> Receipt:
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            r = Receipt.objects.create(
                vendor="Vendor",
                date=date.today(),
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


class AuditRegistryCommandTests(TestCase):
    """`manage.py audit_registry` reports registry rows no doc references."""

    def test_reports_orphans_and_clean_state(self):
        referenced = Project.objects.create(slug="referenced", name="Referenced")
        Project.objects.create(slug="orphan-proj", name="Orphan Project")
        Asset.objects.create(slug="orphan-asset", item_name="Orphan Asset")
        doc = DocumentationRecord.objects.create(doc_id="rb-x", title="X")
        doc.related_projects.add(referenced)

        out = StringIO()
        call_command("audit_registry", "--json", stdout=out)
        stats = json.loads(out.getvalue())

        self.assertEqual(stats["orphan_projects"], ["orphan-proj"])
        self.assertEqual(stats["orphan_assets"], ["orphan-asset"])
        self.assertEqual(stats["projects_total"], 2)
        self.assertEqual(stats["assets_total"], 1)

        # Human output names the orphans and does not claim "ok".
        human = StringIO()
        call_command("audit_registry", stdout=human)
        text = human.getvalue()
        self.assertIn("orphan: orphan-proj", text)
        self.assertNotIn("Registry  ok", text)

    def test_clean_when_everything_referenced(self):
        project = Project.objects.create(slug="p", name="P")
        doc = DocumentationRecord.objects.create(doc_id="rb-y", title="Y")
        doc.related_projects.add(project)

        out = StringIO()
        call_command("audit_registry", stdout=out)
        self.assertIn("Registry  ok", out.getvalue())


class NavigationTests(TestCase):
    """The bar has to say where you are, and only where you are."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user(username="nav")

    def setUp(self):
        self.client.force_login(self.user)

    def _entries(self, url):
        from hq.platform.core.context_processors import nav

        response = self.client.get(url)
        request = response.wsgi_request
        return nav(request)["nav_entries"]

    def test_every_entry_lights_its_own_group_and_no_other(self):
        """Checked for every entry the registry declares, so a new one cannot
        claim someone else's section: an entry with no namespace would
        light its group on every root page, the dashboard included."""

        from django.test import RequestFactory
        from django.urls import NoReverseMatch, resolve

        from hq.platform.application.domains import domain_navigation
        from hq.platform.core.context_processors import nav

        for entry in domain_navigation():
            try:
                url = reverse(entry.route)
            except NoReverseMatch:
                continue
            with self.subTest(route=entry.route):
                request = RequestFactory().get(url)
                request.resolver_match = resolve(url)
                lit = {
                    group["label"]
                    for group in nav(request)["nav_entries"]
                    if group["kind"] == "group" and group["active"]
                }
                self.assertEqual(lit, {entry.group} if entry.group else set())

    def test_only_the_current_page_is_marked_active(self):
        # Every entry in a section shares one namespace, so matching on that would
        # light the whole dropdown at once.
        entries = self._entries(reverse("expenses:list"))
        active = [item["label"] for entry in entries for item in (entry.get("items") or [entry]) if item.get("active")]
        self.assertEqual(active, ["Expenses"])

    def test_the_section_holding_the_page_is_marked_even_with_no_entry(self):
        # The create page has no nav entry; its section must still show. Which
        # group that is comes from the registry rather than being restated here,
        # so regrouping a section stays a one-line change in one file.
        from hq.platform.application.domains import domain_navigation

        holder = next(item.group for item in domain_navigation() if item.route == "expenses:list")
        entries = self._entries(reverse("expenses:create"))
        groups = {entry["label"]: entry for entry in entries if entry["kind"] == "group"}
        self.assertTrue(groups[holder]["active"])
        self.assertEqual([label for label, entry in groups.items() if entry["active"]], [holder])

    def test_system_sorts_after_every_section_that_holds_work(self):
        entries = self._entries(reverse("dashboard"))
        labels = [entry["label"] for entry in entries]
        self.assertEqual(labels[-1], "System")


class CompressedResponseTests(TestCase):
    """A page big enough to compress reaches the server compressed, with one
    Content-Length that is the compressed body's: h11 refuses a response
    carrying two, which would be a 502 on every large page."""

    def test_a_large_page_arrives_compressed_with_one_length(self):
        import gzip

        import httpx2

        from hq.config.asgi import application

        async def fetch():
            transport = httpx2.ASGITransport(app=application, client=("100.64.0.10", 50000))
            async with (
                httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client,
                client.stream("GET", "/accounts/login/", headers={"Accept-Encoding": "gzip"}) as response,
            ):
                # As sent, not as the client would decode it.
                return response, b"".join([chunk async for chunk in response.aiter_raw()])

        response, sent = async_to_sync(fetch)()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-encoding"], "gzip")
        self.assertEqual(len(response.headers.get_list("content-length")), 1)
        self.assertEqual(int(response.headers["content-length"]), len(sent))
        self.assertIn(b"<form", gzip.decompress(sent))


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
