"""Every installed extension's part of a shared page is derived once per change."""

from datetime import date
from unittest import mock

from django.test import TestCase

from hq.platform.application.calendar import CalendarEvent, CalendarSource, gather
from hq.platform.application.dashboard import dashboard_highlights, work_queue
from hq.platform.application.derivations import DERIVATIONS, counting
from hq.platform.application.domains import (
    all_domains,
    composition,
    domain_attention_items,
    domain_dashboard_sections,
)
from hq.platform.application.plugin_testing import (
    ComposedPluginTestCase,
    providers_derived_again,
    sibling,
)
from hq.platform.application.plugins import (
    DERIVED_PROVIDERS,
    PluginIntegration,
    installed_integrations,
    installed_plugins,
    plugin_attention_items,
)
from hq.platform.application.projection import projection_scope
from hq.platform.application.ui import DomainOverview, Insight, Kpi
from hq.platform.core.models import UpstreamReading

from .test_derivations import MOMENT

WINDOW = (date(2026, 1, 1), date(2026, 12, 31))
ITEM = Insight("serious", "Alpha", "Something is wrong", "1", "Body.")
CARDS = (
    {"id": "alpha-open", "label": "Open", "value": 3, "url": "/alpha/"},
    {"id": "alpha-late", "label": "Late", "value": 1, "url": "/alpha/"},
)


def _readings() -> int:
    return UpstreamReading.objects.count()


def _write(key: str = "one") -> None:
    UpstreamReading.objects.create(key=key, value={}, observed_at=MOMENT)


def _events(first, last):
    yield CalendarEvent(id=f"example:{_readings()}", title="Example", starts=first)


def _sources():
    return (CalendarSource(id="example.alpha.reviews", label="Reviews", events=_events),)


OVERVIEW = DomainOverview("Current state.", "/alpha/", (Kpi("Open", 3),))


class ComposedTests(ComposedPluginTestCase, TestCase):
    """An extension is installed beside whatever this suite already loads."""

    siblings = (sibling(cards=CARDS, attention=(ITEM,), overview=OVERVIEW),)

    def setUp(self):
        super().setUp()
        ((manifest, contribution),) = self.siblings
        # The kit's sibling declares no calendar; this one does.
        with_calendar = PluginIntegration(
            dashboard=contribution.dashboard,
            attention=contribution.attention,
            overview=contribution.overview,
            calendars=_sources,
        )
        self._contributions = {manifest.id: with_calendar}
        original = __import__("hq.platform.application.plugins", fromlist=["_import"])._import

        def _import(spec: str):
            module, _, attribute = spec.partition(":")
            if module in self._contributions and attribute == "integration":
                return lambda: self._contributions[module]
            return original(spec)

        patcher = mock.patch("hq.platform.application.plugins._import", side_effect=_import)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _compose(self):
        with projection_scope():
            domain_attention_items()
            work_queue()
            domain_dashboard_sections()
            dashboard_highlights()
            gather(*WINDOW)
            plugin_attention_items()

    def test_every_provider_of_a_shared_composition_is_a_derivation(self):
        self._compose()

        for plugin, integration in installed_integrations():
            for field in DERIVED_PROVIDERS:
                with self.subTest(plugin=plugin.id, provider=field):
                    if getattr(integration, field) is not None:
                        self.assertIn(f"extension.{plugin.id}.{field}", DERIVATIONS)
            for source in integration.calendars() if integration.calendars else ():
                with self.subTest(plugin=plugin.id, source=source.id):
                    self.assertIn(f"extension.{plugin.id}.calendar.{source.id}", DERIVATIONS)

    def test_no_installed_provider_is_computed_twice_for_one_revision(self):
        """The budget: composing every shared page again derives nothing."""

        self._compose()

        with counting() as (ran, served):
            self._compose()

        self.assertEqual(dict(ran), {})
        self.assertIn("extension.example.alpha.calendar.example.alpha.reviews", served)

    def test_every_installed_plugin_is_answered_from_what_is_stored(self):
        for plugin in installed_plugins():
            with self.subTest(plugin=plugin.id):
                self.assertEqual(providers_derived_again(plugin.id, window=WINDOW), [])

    def test_a_write_to_a_table_one_provider_reads_derives_only_what_reads_it(self):
        self._compose()
        _write()

        with counting() as (ran, _served):
            self._compose()

        self.assertEqual(ran["extension.example.alpha.calendar.example.alpha.reviews"], 1)
        self.assertNotIn("extension.example.alpha.attention", ran)
        self.assertNotIn("extension.example.alpha.dashboard", ran)

    def test_the_composition_is_part_of_what_a_composed_answer_varies_by(self):
        named = {domain: field for domain, field, _who in composition()}

        self.assertIn("example.alpha", named)
        self.assertEqual(
            {field for domain, field, _who in composition() if domain == "example.alpha"},
            set(DERIVED_PROVIDERS),
        )
        self.assertTrue(all(domain.origin != "host" or domain.id not in named for domain in all_domains()))

    def test_the_kit_names_a_provider_whose_answer_cannot_be_kept(self):
        manifest = self.siblings[0][0]
        self._contributions[manifest.id] = PluginIntegration(attention=lambda: (lambda: None,))

        with self.assertLogs("severino.derivations", level="ERROR"):
            found = providers_derived_again(manifest.id)

        self.assertEqual(found, ["attention: derived again, its answer was not kept"])

    def test_the_kit_refuses_a_plugin_that_is_not_installed(self):
        with self.assertRaises(LookupError):
            providers_derived_again("example.absent")
