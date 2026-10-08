"""Invariants the domain registry has to hold for the composition to be safe."""

from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from hq.domains.content.models import ContentItem
from hq.domains.expenses.models import Expense
from hq.domains.projects.models import Project

from ..dashboard import work_queue
from ..domains import (
    HOST_DOMAINS,
    HOST_ORDER_FLOOR,
    HOST_ORDER_MACHINERY,
    Domain,
    all_domains,
    domain_attention_items,
    domain_dashboard_cards,
    domain_navigation,
    host_records,
)
from ..plugins import PluginIntegration
from ..ui import Insight


def _pretend_extension_attention() -> tuple[Insight, ...]:
    """Stands in for a private extension, which this public repo cannot import."""

    return (
        Insight(
            status="serious",
            eyebrow="Pretend",
            title="A domain is on fire",
            value="3",
            body="Something an extension believes needs a decision now.",
            action="Look",
            url="/pretend/",
        ),
    )


class DomainRegistryTests(SimpleTestCase):
    def test_every_declared_route_resolves(self):
        """A typo in the registry must fail here, not on every page at once.

        The nav renders in ``base.html``, so an unresolvable route is not a
        broken link: it is a 500 on every authenticated surface in HQ
        simultaneously, including the dashboard an operator would use to notice.
        """
        for item in domain_navigation():
            try:
                reverse(item.route)
            except NoReverseMatch:  # pragma: no cover - the assert reports it
                self.fail(
                    f"Domain nav route {item.route!r} ({item.label}) does not resolve."
                )

    def test_host_sections_never_squat_the_extension_order_band(self):
        """Orders below the floor belong to extensions, which lead the bar.

        A host section numbered into that band would silently push an installed
        extension down the bar, and because this repo cannot see which
        extensions exist, nothing else would catch it.
        """
        for descriptor in HOST_DOMAINS:
            for item in descriptor.navigation:
                if not item.group:
                    # Ungrouped items render inline and are allowed to lead: the
                    # dashboard is the root, not a section competing for a slot.
                    continue
                self.assertGreaterEqual(
                    item.order,
                    HOST_ORDER_FLOOR,
                    f"Host section {item.label!r} squats the extension band.",
                )

    def test_machinery_sorts_after_every_section_that_holds_work(self):
        work = [
            item.order
            for item in domain_navigation()
            if item.order < HOST_ORDER_MACHINERY
        ]
        self.assertTrue(work, "Expected at least one section that holds work.")
        self.assertGreater(HOST_ORDER_MACHINERY, max(work))

    def test_domain_ids_are_unique(self):
        """Ids key attribution on surfaces that compose host and extensions."""
        ids = [domain.id for domain in all_domains()]
        self.assertEqual(sorted(ids), sorted(set(ids)))

    def test_the_registry_is_the_only_list_of_sections(self):
        """No module may keep a second roster of what HQ contains.

        With parallel lists (a nav tuple, a work-queue list, a code-to-URL
        dict), a section present in one and missing from another is a silent
        hole rather than a failure, so the duplication is worth a test rather
        than a convention.
        """
        source = (
            Path(settings.BASE_DIR) / "hq/platform/core" / "context_processors.py"
        ).read_text()
        self.assertNotIn(
            "NAV_ITEMS",
            source,
            "core.context_processors declares sections again; derive them from "
            "application.domains instead.",
        )

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

    def test_record_commands_and_permissions_are_derived_not_listed(self):
        """A record domain's verbs and permissions exist only as its declaration."""
        from ..core_capabilities import CORE_CAPABILITY_SPECS
        from ..security import Capability

        records = host_records()
        verbs = {f"{r.noun}.{verb}" for r in records for verb in ("create", "upsert", "update", "delete")}
        permissions = {p for r in records for p in (r.write, r.delete)}
        self.assertEqual({spec.name for spec in CORE_CAPABILITY_SPECS} & verbs, set())
        self.assertEqual({member.value for member in Capability} & permissions, set())

    def test_the_view_keeps_no_code_to_url_table(self):
        """The queue's links come from the domains, not from a lookup here.

        Each entry carries its own url, so there is no code to look up.
        """
        core = Path(settings.BASE_DIR) / "hq/platform/core"
        for module in sorted({*core.glob("views.py"), *core.glob("*_views.py")}):
            with self.subTest(module=module.name):
                self.assertNotIn("routes = {", module.read_text())


class ComposedQueueTests(TestCase):
    """The queue has to speak for every domain, not just the host's."""

    def test_an_outstanding_item_reaches_the_queue_with_its_own_link(self):
        ContentItem.objects.create(
            title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT
        )

        entry = next(
            item for item in work_queue() if item["source_id"] == "hq.content"
        )

        self.assertEqual(entry["count"], 1)
        # The link the domain supplied, not one this test reconstructs.
        self.assertIn(reverse("content:list"), entry["url"])

    def test_a_section_with_no_data_contributes_no_card(self):
        """The whole of the dormant-section behaviour, with no flag to set.

        A section holding no rows contributes no tile. A figure that is always
        zero on the page an operator reads every day teaches them to skim the
        row it sits in, and the row above it is the work queue.
        """
        labels = [card["label"] for card in domain_dashboard_cards()]
        self.assertFalse(
            [label for label in labels if label.startswith("Expenses")], labels
        )

        Expense.objects.create(
            date=timezone.localdate(),
            vendor="Vendor",
            item="A real expense",
            category="hosting",
            total_cost=Decimal("12.00"),
        )

        # Lights up on its own the moment the section has something to say.
        labels = [card["label"] for card in domain_dashboard_cards()]
        self.assertTrue(
            [label for label in labels if label.startswith("Expenses")], labels
        )

    def test_the_card_row_reads_in_the_same_order_as_the_bar(self):
        ContentItem.objects.create(
            title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT
        )
        Project.objects.create(
            name="Live", slug="live", status=Project.Status.ACTIVE
        )

        cards = domain_dashboard_cards()
        labels = [card["label"] for card in cards]

        # Build (100) before Web (110), matching the nav, so the row does not
        # tell a different story about what matters than the bar above it.
        self.assertLess(labels.index("Active projects"), labels.index("Drafts"))

    def test_extension_items_share_the_queue_with_host_items(self):
        """An extension on fire has to be visible on the page that lists work.

        A queue built only from the host's own models would say "no cleanup
        items" while an extension's domain was failing.
        """
        ContentItem.objects.create(
            title="Half-written", slug="half-written", status=ContentItem.Status.DRAFT
        )
        pretend = Domain(
            id="pretend.domain",
            label="Pretend",
            origin="extension",
            navigation=(),
            integration=PluginIntegration(
                attention=_pretend_extension_attention
            ),
        )

        with patch(
            "hq.platform.application.domains.extension_domains", return_value=(pretend,)
        ):
            entries = domain_attention_items()

        sources = [entry["source_id"] for entry in entries]
        self.assertIn("pretend.domain", sources)
        self.assertIn("hq.content", sources)
        # Most urgent first: the extension's item is "serious", the host's is
        # "attention", so severity decides the order and not who supplied it.
        self.assertEqual(sources[0], "pretend.domain")
