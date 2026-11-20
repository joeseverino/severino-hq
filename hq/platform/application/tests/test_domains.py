"""Invariants the domain registry has to hold for the composition to be safe."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase, tag
from django.urls import reverse
from django.utils import timezone

from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project
from hq.platform.application.security import cli_principal

from ..dashboard import work_queue
from ..domains import (
    HOST_DOMAINS,
    Domain,
    domain_attention_items,
    domain_dashboard_cards,
    host_records,
)
from ..finding_model import Finding, opens
from ..plugins import PluginIntegration


def _pretend_extension_attention() -> tuple[Finding, ...]:
    """Stands in for a private extension, which this public repo cannot import."""

    return (
        Finding(
            rule="pretend-fire",
            subject="",
            severity="serious",
            title="A domain is on fire",
            explanation="Something an extension believes needs a decision now.",
            primary=opens("Look", "/pretend/"),
        ),
    )


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

    def test_record_permissions_are_derived_not_listed(self):
        """A record domain's permissions exist only as its declaration; a verb
        written by hand as well fails the graph's duplicate check."""
        from ..security import Capability

        permissions = {p for r in host_records() for p in (r.write, r.delete)}
        self.assertEqual({member.value for member in Capability} & permissions, set())


class ComposedQueueTests(TestCase):
    """The queue has to speak for every domain, not just the host's."""

    def test_an_outstanding_item_reaches_the_queue_with_its_own_link(self):
        ContentItem.objects.create(title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT)

        entry = next(item for item in work_queue() if item["source_id"] == "hq.content")

        self.assertEqual(entry["count"], 1)
        # The link the domain supplied, not one this test reconstructs.
        self.assertIn(reverse("content:list"), entry["url"])

    def test_a_section_with_no_data_contributes_no_card(self):
        """The whole of the dormant-section behavior, with no flag to set.

        A section holding no rows contributes no tile. A figure that is always
        zero on the page an operator reads every day teaches them to skim the
        row it sits in, and the row above it is the work queue.
        """
        labels = [card["label"] for card in domain_dashboard_cards(cli_principal())]
        self.assertFalse([label for label in labels if label.startswith("Expenses")], labels)

        Expense.objects.create(
            date=timezone.localdate(),
            vendor="Vendor",
            item="A real expense",
            category="hosting",
            total_cost=Decimal("12.00"),
        )

        # Lights up on its own the moment the section has something to say.
        labels = [card["label"] for card in domain_dashboard_cards(cli_principal())]
        self.assertTrue([label for label in labels if label.startswith("Expenses")], labels)

    def test_the_card_row_reads_in_the_same_order_as_the_bar(self):
        ContentItem.objects.create(title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT)
        Project.objects.create(
            name="Live", slug="live", repository_url="https://github.com/example/live", dependency_updates=True
        )

        cards = domain_dashboard_cards(cli_principal())
        labels = [card["label"] for card in cards]

        # Build (100) before Web (110), matching the nav, so the row does not
        # tell a different story about what matters than the bar above it.
        self.assertLess(labels.index("Open updates"), labels.index("Drafts"))

    def test_extension_items_share_the_queue_with_host_items(self):
        """An extension on fire has to be visible on the page that lists work.

        A queue built only from the host's own models would say "no cleanup
        items" while an extension's domain was failing.
        """
        ContentItem.objects.create(title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT)
        pretend = Domain(
            id="pretend.domain",
            label="Pretend",
            origin="extension",
            navigation=(),
            integration=PluginIntegration(attention=_pretend_extension_attention),
        )

        with patch("hq.platform.application.domains.extension_domains", return_value=(pretend,)):
            entries = domain_attention_items()

        sources = [entry["source_id"] for entry in entries]
        self.assertIn("pretend.domain", sources)
        self.assertIn("hq.content", sources)
        # Most urgent first: the extension's item is "serious", the host's is
        # "attention", so severity decides the order and not who supplied it.
        self.assertEqual(sources[0], "pretend.domain")
