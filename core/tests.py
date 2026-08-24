"""Smoke tests for Severino HQ.

These don't aim for exhaustive coverage — they verify that the auth gate works
and every page in the main nav (including reports + exports) renders without a
500. Add module-specific tests inside each app as it grows.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib.auth.middleware import AuthenticationMiddleware
from django.contrib.auth import get_user_model
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from assets.models import Asset
from content.models import ContentItem
from core.middleware import CurrentUserMiddleware, get_current_user, set_current_user
from core.logging import JsonFormatter, reset_request_id, set_request_id
from core.audit import operation_context
from core.models import AuditLog
from core.oidc import HQOIDCAuthenticationBackend
from docs_index.models import DocumentationRecord
from docs_index.importer import (
    ManifestImportError,
    import_manifest_data,
    validate_manifest_data,
)
from expenses.models import Expense
from projects.models import Project
from receipts.models import Receipt


User = get_user_model()


class AuthGateTests(TestCase):
    def test_health_probes_are_anonymous_and_operational(self):
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
        # image, which is the only place it matters -- it passed every gate and
        # failed the composition.
        #
        # What this test is for is that readiness answers anonymously and
        # reports the host as healthy. It is not for how many extensions
        # happen to be composed alongside it.
        for name in ("database", "migrations", "storage"):
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

    def test_anonymous_dashboard_redirects_to_login(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response["Location"])

    def test_auth_redirect_encodes_the_complete_destination(self):
        response = self.client.get("/?filter=open&next=https://example.com")
        query = parse_qs(urlsplit(response["Location"]).query)

        self.assertEqual(query["next"], ["/?filter=open&next=https://example.com"])

    def test_login_page_is_accessible(self):
        response = self.client.get("/accounts/login/")
        self.assertEqual(response.status_code, 200)
        self.assertRegex(response["X-Request-ID"], r"^[0-9a-f]{32}$")

    def test_login_page_enforces_native_content_security_policy(self):
        response = self.client.get("/accounts/login/")
        policy = response.headers["Content-Security-Policy"]

        self.assertIn("default-src 'self'", policy)
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", policy)
        self.assertIn("object-src 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)

    def test_inline_styles_are_the_only_relaxation_in_the_policy(self):
        """Pin the one exception so it stays one exception.

        ``style-src`` allows ``'unsafe-inline'`` because charts position their
        marks with a per-datum custom property -- ``style="--at: 62%"`` -- which
        no class can express and no nonce can cover, because nonces apply to
        style *elements* and not to style *attributes*.

        That is a deliberate, bounded exception. It is asserted here rather
        than merely documented, because the failure mode of a documented rule
        is that a second directive quietly joins the first and the policy is
        weaker than the prose claims. Anything relaxed beyond this fails.
        """

        response = self.client.get("/accounts/login/")
        policy = response.headers["Content-Security-Policy"]

        relaxed = sorted(
            directive.split()[0]
            for directive in policy.split(";")
            if "'unsafe-inline'" in directive or "'unsafe-eval'" in directive
        )
        self.assertEqual(relaxed, ["style-src"])

    def test_application_shell_versions_every_shared_asset(self):
        content = self.client.get("/accounts/login/").content.decode()
        for asset in (
            "css/app.css",
            "img/apple-touch-icon.png",
            "img/favicon.ico",
            "img/favicon.svg",
            "js/app.js",
            "js/tables.js",
        ):
            with self.subTest(asset=asset):
                self.assertRegex(content, rf"/static/{re.escape(asset)}\?v=[0-9a-f]{{12}}")

    @override_settings(SEVERINO_OIDC_ENABLED=True)
    def test_login_page_shows_sso_button_when_enabled(self):
        response = self.client.get("/accounts/login/")
        self.assertContains(response, "Sign in with SSO")


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
    def test_allows_user_in_allowed_group(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS=set(),
            SEVERINO_OIDC_ALLOWED_GROUPS={"admins"},
        ):
            self.assertTrue(
                backend.verify_claims(
                    {
                        "preferred_username": "joe",
                        "groups": ["admins"],
                    }
                )
            )

    def test_matches_existing_user_by_preferred_username_without_email(self):
        user = User.objects.create_user(username="joe")
        backend = HQOIDCAuthenticationBackend()

        users = backend.filter_users_by_claims(
            {"preferred_username": "joe", "groups": ["admins"]}
        )

        self.assertEqual(list(users), [user])

    def test_allows_user_by_allowed_email(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS={"joe@example.com"},
            SEVERINO_OIDC_ALLOWED_GROUPS=set(),
        ):
            self.assertTrue(
                backend.verify_claims(
                    {
                        "email": "joe@example.com",
                        "email_verified": True,
                        "groups": [],
                    }
                )
            )

    def test_rejects_when_no_allow_rule_is_configured(self):
        backend = HQOIDCAuthenticationBackend()

        with override_settings(
            SEVERINO_OIDC_ALLOWED_EMAILS=set(),
            SEVERINO_OIDC_ALLOWED_GROUPS=set(),
        ):
            with self.assertRaises(PermissionDenied):
                backend.verify_claims(
                    {
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
        assert self.client.login(
            username="tester", password="strongtestpass-1234"
        )


class NavigationSmokeTests(_AuthedTestCase):
    URLS = [
        "/",
        "/search/",
        "/projects/",
        "/projects/new/",
        "/content/",
        "/content/new/",
        "/docs/",
        "/docs/new/",
        "/docs/import/",
        "/assets/",
        "/assets/new/",
        "/expenses/",
        "/expenses/new/",
        "/receipts/",
        "/receipts/new/",
        "/reports/",
        "/audit/",
    ]

    def test_all_main_pages_render(self):
        for url in self.URLS:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 200,
                    f"{url} returned {response.status_code}",
                )


class SearchPageTests(_AuthedTestCase):
    def test_empty_search_is_the_derived_command_center(self):
        response = self.client.get("/search/")

        content = response.content.decode()
        self.assertContains(response, "Command Center")
        self.assertIn("project.create", content)
        self.assertIn("infrastructure.controllers", content)
        self.assertIn('href="/projects/"', content)
        self.assertIn('href="/commands/project.create/"', content)
        self.assertIn(
            '<a class="discovery-link" href="/commands/project.create/">', content
        )
        self.assertNotIn("Open command", content)

    def test_query_filters_commands_as_well_as_records(self):
        response = self.client.get("/search/", {"q": "certificate.renew"})

        content = response.content.decode()
        self.assertIn("certificate.renew", content)
        self.assertNotIn("project.create", content)
        self.assertNotIn("No resources match this query.", content)

    def test_results_highlight_matches_and_skip_empty_scopes(self):
        from unittest import mock

        Project.objects.create(
            name="Highlight target",
            description="global search rendering check",
        )
        with mock.patch("core.views.search_submissions", return_value=[]):
            response = self.client.get("/search/", {"q": "highlight"})

        content = response.content.decode()
        self.assertIn("<mark>", content)
        self.assertIn("Highlight target", content)
        # Empty scopes are omitted entirely, not rendered as empty cards.
        self.assertNotIn("No matches.", content)
        self.assertNotIn("Expenses</h2>", content)


class DashboardWorkflowTests(_AuthedTestCase):
    def test_action_items_is_the_full_existing_queue_and_filters_it(self):
        Project.objects.create(name="Documentable lab", status=Project.Status.ACTIVE)

        with patch("application.attention.get_unread_count", return_value=0):
            response = self.client.get("/action-items/")
            filtered = self.client.get(
                "/action-items/", {"status": "serious", "q": "project"}
            )

        self.assertContains(response, "Active projects need output")
        self.assertContains(response, 'href="/projects/?needs_output=1"')
        self.assertContains(response, "Action items")
        self.assertNotContains(filtered, "Active projects need output")

    def test_profile_count_is_lazy_off_dashboard(self):
        Project.objects.create(name="Count me", status=Project.Status.ACTIVE)

        page = self.client.get("/projects/")
        with (
            patch("application.attention.get_unread_count", return_value=0),
            patch("application.domains.extension_domains", return_value=()),
        ):
            count = self.client.get("/action-items/count/")

        self.assertContains(page, 'data-action-count hidden')
        self.assertEqual(count.json()["count"], 1)

    def test_dashboard_uses_one_combined_contact_read(self):
        state = (
            [
                {
                    "id": 7,
                    "name": "One call",
                    "status": "unread",
                    "created_at": "2026-08-23",
                }
            ],
            3,
        )
        with (
            patch("core.views.get_dashboard_state", return_value=state) as combined,
            patch("application.attention.get_unread_count") as separate,
        ):
            response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        combined.assert_called_once_with(limit=4)
        separate.assert_not_called()
        self.assertContains(response, "One call")
        self.assertContains(response, "3")

    def test_dashboard_routes_infrastructure_findings_to_their_evidence(self):
        from control_plane.models import ManagedResource

        ManagedResource.objects.create(
            key="jseverino-wildcard",
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

        response = self.client.get("/")

        self.assertContains(response, "Infrastructure findings")
        self.assertContains(response, "/infrastructure/findings/")

    def test_dashboard_surfaces_missing_project_output_once_in_queue(self):
        Project.objects.create(name="Documentable lab", status=Project.Status.ACTIVE)

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open items")
        self.assertContains(response, "Priority queue")
        self.assertContains(response, "Active projects need output")
        self.assertContains(response, "Documentable lab")
        self.assertContains(response, "/projects/?needs_output=1")
        self.assertNotContains(response, "Needs attention")
        self.assertNotContains(response, "Project opportunities")
        self.assertNotContains(response, "Relationship health")
        self.assertNotContains(response, "Docs by system")

    def test_dashboard_excludes_site_pages_from_docs_review(self):
        DocumentationRecord.objects.create(
            doc_id="page-about",
            title="About page",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            status=DocumentationRecord.Status.ACTIVE,
        )

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "All active docs reviewed recently.")
        self.assertNotContains(response, "Review queue")

    def test_projects_can_filter_for_missing_output(self):
        project = Project.objects.create(
            name="No output yet", status=Project.Status.ACTIVE
        )
        documented = Project.objects.create(
            name="Documented", status=Project.Status.ACTIVE
        )
        doc = DocumentationRecord.objects.create(
            doc_id="rb-documented-001",
            title="Documented runbook",
            status=DocumentationRecord.Status.ACTIVE,
        )
        doc.related_projects.add(documented)
        content = ContentItem.objects.create(title="Documented post")
        content.related_projects.add(documented)

        response = self.client.get("/projects/?needs_output=1")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, project.name)
        self.assertNotContains(response, documented.name)

    def test_archived_projects_sort_last_by_default(self):
        active = Project.objects.create(
            name="ZZZ active project", status=Project.Status.ACTIVE
        )
        archived = Project.objects.create(
            name="AAA archived project", status=Project.Status.ARCHIVED
        )

        response = self.client.get("/projects/")

        self.assertEqual(response.status_code, 200)
        body = response.content.decode("utf-8")
        self.assertLess(body.index(active.name), body.index(archived.name))

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
        self.assertCountEqual(response.context["projects"], [active_lab, idea_lab])
        status_filter = next(
            item for item in response.context["table"]["filters"]
            if item["name"] == "status"
        )
        self.assertEqual(status_filter["selected_count"], 2)

    def test_table_sort_is_allowlisted(self):
        Project.objects.create(name="Safe sort")

        response = self.client.get("/projects/", {"sort": "not_a_model_field"})

        self.assertEqual(response.status_code, 200)

    def test_table_search_combines_terms_across_searchable_fields(self):
        matching = Project.objects.create(
            name="Operations console",
            technologies_used="Django SQLite",
        )
        Project.objects.create(name="Operations notes", technologies_used="Markdown")

        response = self.client.get("/projects/", {"q": "operations django"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context["projects"]), [matching])

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

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, stale.title)
        self.assertNotContains(response, current.title)
        self.assertNotContains(response, site_page.title)

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
            [record.doc_id for record in response.context["docs_needing_review"]],
            [stale.doc_id],
        )


class ExportSmokeTests(_AuthedTestCase):
    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name="Lab — homelab DNS", status=Project.Status.ACTIVE
        )
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
        self.content = ContentItem.objects.create(
            title="Test article", status=ContentItem.Status.DRAFT
        )

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
                    "related_projects": [project.slug],
                    "tags": ["sqlite", "tailscale"],
                }
            ]
        )

        project.refresh_from_db()
        self.assertEqual(project.technologies_used, "Django, PostgreSQL")
        self.assertNotIn("projects_tech_backfilled", stats)

    def test_task_doc_imports_with_its_own_status_lifecycle(self):
        # A task carries open/active/parked/done/wontfix — the standard status
        # set rejects these, so the importer must validate tasks per-doc-type
        # (the bug that wedged `hq sync`: every open task failed validation).
        stats = import_manifest_data(
            [
                {
                    "doc_id": "task-ship-the-thing",
                    "title": "Ship the thing",
                    "doc_type": "task",
                    "status": "open",
                    "related_projects": [],
                }
            ]
        )
        self.assertEqual(stats["created"], 1)
        record = DocumentationRecord.objects.get(doc_id="task-ship-the-thing")
        self.assertEqual(record.status, "open")

    def test_task_status_set_is_distinct_from_standard_docs(self):
        # "parked" is a task status, never a standard-doc status; "deprecated" is
        # the reverse. Each doc_type is held to its own vocabulary.
        with self.assertRaises(ManifestImportError):
            import_manifest_data(
                [{"doc_id": "rb-x", "title": "x", "doc_type": "runbook", "status": "parked"}]
            )
        with self.assertRaises(ManifestImportError):
            import_manifest_data(
                [{"doc_id": "task-x", "title": "x", "doc_type": "task", "status": "deprecated"}]
            )

    def test_validate_manifest_data_flags_bad_enums_without_db(self):
        # The read-only preflight catches the contract-drift class (an invalid
        # status/doc_type/environment) before it reaches the deployed importer,
        # and writes nothing.
        problems = validate_manifest_data([
            {"doc_id": "rb-ok", "title": "ok", "doc_type": "runbook", "status": "active"},
            {"doc_id": "task-bad", "title": "bad", "doc_type": "task", "status": "deprecated"},
            {"doc_id": "rb-bad-env", "doc_type": "runbook", "environment": "nope"},
        ])
        by = {p["doc_id"]: p for p in problems}
        self.assertNotIn("rb-ok", by)
        self.assertIn("task-bad", by)
        self.assertIn("rb-bad-env", by)
        self.assertEqual(DocumentationRecord.objects.count(), 0)

    def test_check_only_command_gates_an_invalid_manifest(self):
        bad = json.dumps([{"doc_id": "task-x", "title": "x", "doc_type": "task", "status": "deprecated"}])
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(bad)
            bad_path = f.name
        ok = json.dumps([{"doc_id": "task-x", "title": "x", "doc_type": "task", "status": "open"}])
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
                    "doc_id": "writeup-custom-mcp-layer",
                    "slug": "building-a-custom-mcp-layer",
                    "title": "Building a Custom MCP Layer",
                    "doc_type": DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
                    "system": "jseverino.com",
                    "environment": DocumentationRecord.Environment.CLOUDFLARE,
                    "status": DocumentationRecord.Status.DRAFT,
                    "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                    "content_type": "portfolio_article",
                    "published": False,
                    "path": "05 Writeups/building-a-custom-mcp-layer/index.md",
                }
            ]
        )

        record = DocumentationRecord.objects.get(doc_id="writeup-custom-mcp-layer")
        item = ContentItem.objects.get(slug="building-a-custom-mcp-layer")

        self.assertTrue(item.related_documentation.filter(pk=record.pk).exists())
        self.assertFalse(ContentItem.objects.filter(slug="custom-mcp-layer").exists())

    def test_public_article_without_content_type_prunes_legacy_content_item(self):
        record = DocumentationRecord.objects.create(
            doc_id="report-platform-playbook-public",
            title="Severino Labs Platform Playbook",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
        )
        stale_item = ContentItem.objects.create(
            slug="report-platform-playbook-public",
            title="Severino Labs Platform Playbook",
            content_type=ContentItem.Type.PORTFOLIO_PAGE,
            status=ContentItem.Status.DRAFT,
        )
        stale_item.related_documentation.add(record)

        stats = import_manifest_data(
            [
                {
                    "doc_id": "report-platform-playbook-public",
                    "title": "Severino Labs Platform Playbook",
                    "doc_type": DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
                    "system": "Severino Labs (cross-cutting)",
                    "environment": DocumentationRecord.Environment.OTHER,
                    "status": DocumentationRecord.Status.ACTIVE,
                    "sensitivity": DocumentationRecord.Sensitivity.INTERNAL,
                    "path": "02 Infrastructure/00 Reporting/Severino Labs Platform Playbook.md",
                }
            ]
        )

        self.assertEqual(stats["content_items_pruned"], 1)
        self.assertFalse(
            ContentItem.objects.filter(slug="report-platform-playbook-public").exists()
        )

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


class DeductibleMathTests(TestCase):
    def test_expense_deductible_is_recomputed_on_save(self):
        e = Expense.objects.create(
            date=date.today(),
            vendor="x",
            item="y",
            category="hosting",
            total_cost=Decimal("100.00"),
            business_use_percentage=75,
        )
        self.assertEqual(e.estimated_deductible_amount, Decimal("75.00"))
        e.total_cost = Decimal("200.00")
        e.save()
        self.assertEqual(e.estimated_deductible_amount, Decimal("150.00"))

    def test_asset_deductible_clamps_percentage(self):
        a = Asset.objects.create(
            item_name="x",
            total_cost=Decimal("100.00"),
            business_use_percentage=250,  # nonsense
        )
        # Saved value is clamped to 100.
        self.assertEqual(a.business_use_percentage, 100)
        self.assertEqual(a.estimated_deductible_amount, Decimal("100.00"))


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

    def test_an_update_records_which_fields_moved_and_to_what(self):
        project = Project.objects.create(name="Before", status=Project.Status.IDEA)
        project.name = "After"
        project.status = Project.Status.ACTIVE
        project.save()

        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).first()
        changes = event.metadata["changes"]
        self.assertEqual(changes["name"], ["Before", "After"])
        self.assertEqual(changes["status"], ["idea", "active"])

    def test_a_save_that_changed_nothing_is_not_an_event(self):
        # Django writes every column on every save, so a form re-submitted
        # unchanged used to leave a row saying "Updated" and meaning nothing.
        project = Project.objects.create(name="Steady", status=Project.Status.IDEA)
        before = AuditLog.objects.count()

        project.save()

        self.assertEqual(AuditLog.objects.count(), before)

    def test_a_second_save_diffs_against_the_first_not_the_original(self):
        project = Project.objects.create(name="One", status=Project.Status.IDEA)
        project.name = "Two"
        project.save()
        project.name = "Three"
        project.save()

        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).first()
        self.assertEqual(event.metadata["changes"]["name"], ["Two", "Three"])

    def test_values_that_are_not_json_survive_the_round_trip(self):
        # A Decimal, a date and a UUID are none of the things JSON has. Left
        # as they are the row fails to write, `record_event` swallows it, and
        # the change is lost with nothing saying so.
        expense = Expense.objects.create(
            date=date.today(), vendor="V", item="I", total_cost=Decimal("1.00")
        )
        expense.total_cost = Decimal("2.50")
        expense.save()

        event = AuditLog.objects.filter(
            action=AuditLog.Action.UPDATED, object_type="Expense"
        ).first()
        self.assertIsNotNone(event, "the row must be written, not swallowed")
        self.assertEqual(event.metadata["changes"]["total_cost"], ["1.00", "2.50"])

    def test_a_redacted_field_reports_the_change_without_the_value(self):
        from core.audit import REDACTED, _changes

        changes = _changes(
            {"token": "old-secret"}, {"token": "new-secret"}, frozenset({"token"})
        )

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

        self.assertContains(page, "What changed")
        self.assertContains(page, "Before")
        self.assertContains(page, "After")

    def test_an_event_links_to_the_rest_of_its_operation(self):
        # The point of operation_id: one action touching several rows is one
        # thing that happened, not four events that share a timestamp.
        with operation_context(
            interface="cli", actor="op", operation="test.bulk", operation_id="op-1"
        ):
            Project.objects.create(name="One", status=Project.Status.IDEA)
            Project.objects.create(name="Two", status=Project.Status.IDEA)
        event = AuditLog.objects.filter(operation_id="op-1").first()

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        self.assertContains(page, "Rest of this operation")

    def test_an_event_shows_what_else_happened_to_the_same_object(self):
        project = Project.objects.create(name="Tracked", status=Project.Status.IDEA)
        project.name = "Renamed"
        project.save()
        event = AuditLog.objects.filter(action=AuditLog.Action.UPDATED).first()

        page = self.client.get(reverse("core:audit_detail", args=[event.pk]))

        self.assertContains(page, "Everything else to this object")

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
            path = Path(r.file.storage.location) / "receipts" / "test.txt"
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
        from core.context_processors import nav

        response = self.client.get(url)
        request = response.wsgi_request
        return nav(request)["nav_entries"]

    def test_only_the_current_page_is_marked_active(self):
        # Every entry in a section shares one namespace, so matching on that lit
        # the whole dropdown at once.
        entries = self._entries(reverse("expenses:list"))
        active = [
            item["label"]
            for entry in entries
            for item in (entry.get("items") or [entry])
            if item.get("active")
        ]
        self.assertEqual(active, ["Expenses"])

    def test_the_section_holding_the_page_is_marked_even_with_no_entry(self):
        # The create page has no nav entry; its section must still show. Which
        # group that is comes from the registry rather than being restated here,
        # so regrouping a section stays a one-line change in one file.
        from application.domains import domain_navigation

        holder = next(
            item.group for item in domain_navigation() if item.route == "expenses:list"
        )
        entries = self._entries(reverse("expenses:create"))
        groups = {entry["label"]: entry for entry in entries if entry["kind"] == "group"}
        self.assertTrue(groups[holder]["active"])
        self.assertEqual(
            [label for label, entry in groups.items() if entry["active"]], [holder]
        )

    def test_system_sorts_after_every_section_that_holds_work(self):
        entries = self._entries(reverse("dashboard"))
        labels = [entry["label"] for entry in entries]
        self.assertEqual(labels[-1], "System")


class CompressedResponseTests(TestCase):
    """The compressed path has to hand the server one Content-Length.

    Django emits header names in the case they were set; ASGI says lowercase,
    and Starlette's compressor matches on lowercase. Mismatched, it appends a
    second Content-Length rather than replacing the first, and h11 refuses to
    send a response carrying two -- a 502 on every page big enough to compress.
    """

    def _send_through(self, app, headers, body):
        async def django_like(scope, receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": headers,
                }
            )
            await send({"type": "http.response.body", "body": body})

        sent = []

        async def collect(message):
            sent.append(message)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"accept-encoding", b"gzip")],
        }
        async_to_sync(app(django_like))(scope, receive, collect)
        return sent

    def _compressible(self):
        body = b"<p>severino</p>" * 200
        return [
            (b"Content-Type", b"text/html; charset=utf-8"),
            (b"Content-Length", str(len(body)).encode()),
            (b"Vary", b"Cookie"),
        ], body

    def test_a_compressed_response_carries_exactly_one_content_length(self):
        from starlette.middleware.gzip import GZipMiddleware

        from core.headers import LowercaseHeaders

        headers, body = self._compressible()
        sent = self._send_through(
            lambda app: GZipMiddleware(LowercaseHeaders(app), minimum_size=1000),
            headers,
            body,
        )

        start = next(m for m in sent if m["type"] == "http.response.start")
        lengths = [v for name, v in start["headers"] if name.lower() == b"content-length"]
        self.assertEqual(len(lengths), 1)
        self.assertEqual(int(lengths[0]), len(sent[-1]["body"]))

    def test_the_unwrapped_compressor_is_what_produced_two(self):
        # Pins the cause rather than the symptom: without the normalisation,
        # the same response goes out with conflicting lengths.
        from starlette.middleware.gzip import GZipMiddleware

        headers, body = self._compressible()
        sent = self._send_through(
            lambda app: GZipMiddleware(app, minimum_size=1000), headers, body
        )

        start = next(m for m in sent if m["type"] == "http.response.start")
        lengths = [v for name, v in start["headers"] if name.lower() == b"content-length"]
        self.assertEqual(len(lengths), 2)

    def test_every_header_name_reaching_the_server_is_lowercase(self):
        from core.headers import LowercaseHeaders

        headers, body = self._compressible()
        sent = self._send_through(LowercaseHeaders, headers, body)

        start = next(m for m in sent if m["type"] == "http.response.start")
        self.assertEqual(
            [name for name, _ in start["headers"]],
            [name.lower() for name, _ in headers],
        )
