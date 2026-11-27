"""Invariants the domain registry has to hold for the composition to be safe."""

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag

from hq.domains.content.models import ContentItem
from hq.domains.projects.models import Project
from hq.platform.application.security import cli_principal

from ..domains import (
    domain_dashboard_cards,
)
from ..host_sections import HOST_DOMAINS


class DomainRegistryTests(SimpleTestCase):
    @tag("source")
    def test_apps_and_urls_are_read_off_the_declarations(self):
        """settings and the root urlconf name no domain; each declares itself."""
        root = Path(settings.BASE_DIR) / "hq/config"
        settings_source = (root / "settings.py").read_text()
        urls_source = (root / "urls.py").read_text()
        for descriptor in HOST_DOMAINS:
            for app in descriptor.apps:
                self.assertIn(app, settings.INSTALLED_APPS)
                self.assertNotIn(f'"{app}"', settings_source)
            for mount in descriptor.mounts:
                self.assertNotIn(f'"{mount.urlconf}"', urls_source)


class ComposedQueueTests(TestCase):
    """The queue has to speak for every domain, not just the host's."""

    def test_the_card_row_reads_in_the_same_order_as_the_bar(self):
        ContentItem.objects.create(title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT)
        Project.objects.create(
            name="Live", slug="live", repository_url="https://github.com/example/live", dependency_updates=True
        )

        cards = domain_dashboard_cards(cli_principal())
        ids = [card["id"] for card in cards]

        # Build (100) before Web (110), matching the nav, so the row does not
        # tell a different story about what matters than the bar above it.
        self.assertLess(ids.index("hq.projects.updates"), ids.index("hq.content.drafts"))
