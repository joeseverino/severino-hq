"""Tests for the jseverino.com content-index sync."""

from __future__ import annotations

import datetime
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from content.content_sync import (
    ContentSyncError,
    fetch_content_index,
    sync_content_index,
)
from content.models import ContentItem
from projects.models import Project


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
                "url": "https://jseverino.com/portfolio/zero-trust-private-infrastructure/",
            },
            {
                "slug": "building-a-homelab",
                "title": "Building a Homelab",
                "description": "A retired OptiPlex turned into a private homelab.",
                "published_at": "2026-05-06T00:00:00.000Z",
                "technologies": ["docker", "tailscale"],
                "url": "https://jseverino.com/portfolio/building-a-homelab/",
            },
        ],
    }


class ContentSyncTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="jseverino.com Astro Site", slug="jseverino-site"
        )

    def test_creates_items_and_relates_to_project(self):
        stats = sync_content_index(payload=_payload())

        self.assertEqual(stats["created"], 2)
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["project"], "jseverino-site")
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
        # Manual classification is preserved — sync sets content_type on create only.
        self.assertEqual(item.content_type, ContentItem.Type.CASE_STUDY)

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

            def read(self):
                return json.dumps(_payload()).encode()

        with patch("urllib.request.urlopen", return_value=Response()) as open_url:
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
