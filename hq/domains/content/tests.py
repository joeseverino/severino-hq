"""Tests for the content-index sync."""

import datetime
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from hq.domains.content.content_sync import (
    ContentSyncError,
    fetch_content_index,
    sync_content_index,
)
from hq.domains.content.models import ContentItem
from hq.domains.projects.models import Project


def _payload():
    return {
        "count": 2,
        "items": [
            {
                "slug": "zero-trust-private-infrastructure",
                "title": "Zero-Trust Private Infrastructure",
                "description": "A private cloud and homelab architecture.",
                "published_at": "2026-05-10T00:00:00.000Z",
                "technologies": ["tailscale", "caddy", "nftables"],
                "url": "https://example.com/portfolio/zero-trust-private-infrastructure/",
            },
            {
                "slug": "building-a-homelab",
                "title": "Building a Homelab",
                "description": "A spare PC turned into a private homelab.",
                "published_at": "2026-05-06T00:00:00.000Z",
                "technologies": ["docker", "tailscale"],
                "url": "https://example.com/portfolio/building-a-homelab/",
            },
        ],
    }


@override_settings(
    CONTENT_INDEX_URL="https://example.com/content-index.json",
    CONTENT_INDEX_PROJECT_SLUG="example-site",
)
class ContentSyncTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Example Site", slug="example-site"
        )

    def test_creates_items_and_relates_to_project(self):
        stats = sync_content_index(payload=_payload())

        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["project"], "example-site")
        self.assertEqual(ContentItem.objects.count(), 2)

        item = ContentItem.objects.get(slug="zero-trust-private-infrastructure")
        self.assertEqual(item.status, ContentItem.Status.PUBLISHED)
        self.assertEqual(item.published_at, datetime.date(2026, 5, 10))
        self.assertEqual(item.tags, "tailscale, caddy, nftables")
        self.assertIn(self.project, item.related_projects.all())

    def test_is_idempotent(self):
        sync_content_index(payload=_payload())
        stats = sync_content_index(payload=_payload())

        self.assertEqual(stats["created"], 0)
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(ContentItem.objects.count(), 2)

    def test_updates_changed_fields_without_touching_content_type(self):
        sync_content_index(payload=_payload())
        item = ContentItem.objects.get(slug="building-a-homelab")
        item.content_type = ContentItem.Type.CASE_STUDY  # manual classification
        item.save(update_fields=["content_type"])

        changed = _payload()
        changed["items"][1]["title"] = "Building a Homelab (updated)"
        stats = sync_content_index(payload=changed)

        item.refresh_from_db()
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(item.title, "Building a Homelab (updated)")
        # Manual classification is preserved: sync sets content_type on create only.
        self.assertEqual(item.content_type, ContentItem.Type.CASE_STUDY)

    def test_entries_without_a_slug_are_skipped_and_blanks_are_derived(self):
        payload = {
            "items": [
                "not an entry",
                {"slug": "  ", "title": "No slug"},
                {
                    "slug": " bare ",
                    "description": "  " + "d" * 200,
                    "technologies": ["a", "", None, "b"],
                    "published_at": "not a date",
                },
            ]
        }

        stats = sync_content_index(payload=payload)

        self.assertEqual(stats, {"created": 1, "updated": 0, "total": 1, "project": "example-site"})
        item = ContentItem.objects.get()
        self.assertEqual(item.slug, "bare")
        self.assertEqual(item.title, "bare")
        self.assertEqual(item.topic, "d" * 200)
        self.assertEqual(item.tags, "a, b")
        self.assertEqual(item.published_url, "")
        self.assertIsNone(item.published_at)
        self.assertEqual(item.content_type, ContentItem.Type.LAB_WRITEUP)

    @override_settings(CONTENT_INDEX_PROJECT_SLUG="missing-project")
    def test_no_project_relates_nothing(self):
        stats = sync_content_index(payload=_payload())

        self.assertIsNone(stats["project"])
        self.assertFalse(ContentItem.objects.filter(related_projects__isnull=False).exists())

    @override_settings(
        CF_ACCESS_CLIENT_ID="client-id",
        CF_ACCESS_CLIENT_SECRET="client-secret",
    )
    def test_the_access_token_never_follows_a_redirect_to_another_origin(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = []

        class Elsewhere(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        elsewhere = HTTPServer(("127.0.0.1", 0), Elsewhere)

        class Index(Elsewhere):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{elsewhere.server_port}/steal")
                self.end_headers()

        index = HTTPServer(("127.0.0.1", 0), Index)
        for server in (index, elsewhere):
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)

        with self.assertRaises(ContentSyncError):
            fetch_content_index(f"http://127.0.0.1:{index.server_port}/content-index.json")

        self.assertEqual(seen, [])

    def test_an_index_larger_than_an_index_is_refused_unread(self):
        class Response:
            requested = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, limit=-1):
                Response.requested = limit
                return b" " * (limit if limit > 0 else 64 * 1024 * 1024)

        with (
            patch("hq.domains.content.content_sync._OPENER.open", return_value=Response()),
            self.assertRaisesMessage(ContentSyncError, "larger than an index"),
        ):
            fetch_content_index("https://example.test/content-index.json")

        self.assertGreater(Response.requested, 0)

    def test_missing_items_list_raises(self):
        with self.assertRaises(ContentSyncError):
            sync_content_index(payload={"nope": True})

    @override_settings(
        CF_ACCESS_CLIENT_ID="client-id",
        CF_ACCESS_CLIENT_SECRET="client-secret",
    )
    def test_fetch_identifies_hq_and_sends_access_credentials(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_limit):
                return json.dumps(_payload()).encode()

        with patch("hq.domains.content.content_sync._OPENER.open", return_value=Response()) as open_url:
            payload = fetch_content_index("https://example.test/content-index.json")

        request = open_url.call_args.args[0]
        self.assertEqual(request.get_header("Accept"), "application/json")
        self.assertEqual(
            request.get_header("User-agent"),
            "Severino-HQ/1.0 (+https://github.com/joeseverino/severino-hq)",
        )
        self.assertEqual(request.get_header("Cf-access-client-id"), "client-id")
        self.assertEqual(request.get_header("Cf-access-client-secret"), "client-secret")
        self.assertEqual(payload["count"], 2)


class ContentPageTests(TestCase):
    """What a writeup's or page's own page calls things, and where it leads."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.client.force_login(get_user_model().objects.create_superuser("content-reader"))

    def test_a_draft_with_a_date_calls_it_planned(self):
        draft = ContentItem.objects.create(
            title="Example draft", slug="example-draft", published_at=datetime.date(2030, 1, 2)
        )
        live = ContentItem.objects.create(
            title="Example writeup",
            slug="example-writeup",
            status=ContentItem.Status.PUBLISHED,
            published_at=datetime.date(2030, 1, 2),
            published_url="https://example.com/portfolio/example-writeup/",
        )

        drafted = self.client.get(draft.get_absolute_url())
        published = self.client.get(live.get_absolute_url())

        self.assertContains(drafted, "<dt>Planned date</dt>")
        self.assertNotContains(drafted, "<dt>Published</dt>")
        self.assertContains(published, "<dt>Published</dt>")
        self.assertContains(published, "Open live page")
        self.assertNotContains(published, "<dt>Slug</dt>")
        self.assertContains(published, "<dt>Added to HQ</dt>")

    def _published(self, slug: str, url: str) -> ContentItem:
        return ContentItem.objects.create(
            title=slug, slug=slug, status=ContentItem.Status.PUBLISHED, published_url=url
        )

    def test_the_writeups_list_says_once_where_it_is_published(self):
        from django.urls import reverse

        site = Project.objects.create(name="Example Site", slug="example-site", public_url="https://example.com/")
        self._published("one", "https://example.com/portfolio/one/")
        self._published("two", "https://example.com/portfolio/two/")
        ContentItem.objects.create(title="A draft", slug="a-draft")

        response = self.client.get(reverse("content:writeups"))

        self.assertContains(
            response,
            f'<p class="muted">Published on <a href="{site.get_absolute_url()}" data-entity="Project">example.com</a></p>',
            html=True,
        )
        self.assertEqual(response.content.decode().count("Published on"), 1)

    def test_a_site_no_project_publishes_is_named_without_a_page(self):
        from django.urls import reverse

        self._published("one", "https://example.com/portfolio/one/")

        response = self.client.get(reverse("content:writeups"))

        self.assertContains(response, 'Published on <span data-entity="Site">example.com</span>', html=True)

    def test_two_sites_or_none_are_not_said(self):
        from django.urls import reverse

        empty = self.client.get(reverse("content:writeups"))
        self._published("one", "https://example.com/portfolio/one/")
        self._published("two", "https://example.org/two/")

        both = self.client.get(reverse("content:writeups"))

        self.assertNotContains(empty, "Published on")
        self.assertNotContains(both, "Published on")

    def test_its_own_vault_note_is_its_source_document(self):
        from hq.domains.docs_index.models import DocumentationRecord

        item = ContentItem.objects.create(title="Example writeup", slug="example-writeup")
        note = DocumentationRecord.objects.create(
            doc_id="writeup-example",
            title="Example writeup",
            doc_type=DocumentationRecord.DocType.PUBLIC_ARTICLE_DRAFT,
            obsidian_path="05 Writeups/example-writeup/index.md",
        )
        runbook = DocumentationRecord.objects.create(doc_id="rb-example-publish", title="Publish an example")
        item.related_documentation.add(note, runbook)

        response = self.client.get(item.get_absolute_url())

        self.assertContains(response, "<h3>Source document</h3>")
        self.assertContains(response, 'title="writeup-example">05 Writeups/example-writeup/index.md</a>')
        self.assertContains(response, 'title="rb-example-publish">Publish an example</a>')
        self.assertNotContains(response, "writeup-example ·")

    def test_each_list_names_what_it_adds(self):
        from django.urls import reverse

        ContentItem.objects.create(title="Example writeup", slug="example-writeup")

        writeups = self.client.get(reverse("content:writeups"))
        pages = self.client.get(reverse("content:pages"))
        everything = self.client.get(reverse("content:list"))

        self.assertContains(writeups, "New writeup")
        # One type on the list: the column and its filter say nothing.
        self.assertNotIn("Type", [column.label for column in writeups.context["table"]["columns"]])
        self.assertContains(pages, 'No pages yet. <a href="/content/new/?content_type=page">New page</a>')
        self.assertContains(everything, "<h1>Content</h1>")
        self.assertContains(everything, "New writeup or page")
        self.assertContains(everything, "No source document")
        for gone in ("Content pipeline", "New content item", "Missing documentation", "reverse"):
            self.assertNotContains(everything, gone)

    def test_the_form_opened_from_a_list_is_about_that_lists_kind(self):
        from django.urls import reverse

        response = self.client.get(reverse("content:create"), {"content_type": "page"})

        self.assertContains(response, "<h1>New page</h1>")
        self.assertContains(response, "Save page")
        self.assertEqual(response.context["form"].initial["content_type"], "page")
