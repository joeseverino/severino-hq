"""Tests for the jseverino.com content-index sync."""

from __future__ import annotations

import datetime

from django.test import TestCase

from content.content_sync import ContentSyncError, sync_content_index
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
