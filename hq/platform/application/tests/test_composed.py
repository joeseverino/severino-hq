"""Every installed extension's part of a shared page is derived once per change."""

from dataclasses import replace
from datetime import date

from django.test import TestCase

from hq.platform.application.calendar import CalendarEvent, CalendarSource, gather
from hq.platform.application.card_items import Counted, Kpi
from hq.platform.application.dashboard import dashboard_highlights, work_queue
from hq.platform.application.derivation_testing import frozen
from hq.platform.application.derivations import counting
from hq.platform.application.domains import (
    domain_attention_items,
    domain_dashboard_sections,
)
from hq.platform.application.finding_model import Finding
from hq.platform.application.finding_steps import Do
from hq.platform.application.plugin_testing import (
    ComposedPluginTestCase,
    installed,
    providers_derived_again,
    sibling,
)
from hq.platform.application.plugins import (
    installed_plugins,
    plugin_attention_items,
)
from hq.platform.application.projection import projection_scope
from hq.platform.application.security import cli_principal
from hq.platform.application.ui import DomainOverview
from hq.platform.core.models import UpstreamReading

from .test_derivations import MOMENT

WINDOW = (date(2026, 1, 1), date(2026, 12, 31))
ITEM = Finding(
    rule="alpha-wrong",
    subject="",
    severity="serious",
    title="Something is wrong",
    explanation="Body.",
    steps=(Do("Look into it."),),
)
CARDS = (
    {"id": "alpha-open", "label": Counted(3, "Open item", "Open items"), "url": "/alpha/"},
    {"id": "alpha-late", "label": Counted(1, "Late item", "Late items"), "url": "/alpha/"},
)


def _readings() -> int:
    return UpstreamReading.objects.count()


def _write(key: str = "one") -> None:
    UpstreamReading.objects.create(key=key, value={}, observed_at=MOMENT)


def _events(first, last):
    yield CalendarEvent(id=f"example:{_readings()}", title="Example", starts=first)


def _sources():
    return (CalendarSource(id="example.alpha.reviews", label="Reviews", events=_events),)


OVERVIEW = DomainOverview("Current state.", "/alpha/", (Kpi("Open", "3"),))


def _with_calendar(manifest, contribution):
    """The kit's sibling declares no calendar; this one does."""

    return manifest, replace(contribution, calendars=_sources)


class ComposedTests(ComposedPluginTestCase, TestCase):
    """An extension is installed beside whatever this suite already loads."""

    siblings = (_with_calendar(*sibling(cards=CARDS, attention=(ITEM,), overview=OVERVIEW)),)

    def setUp(self):
        super().setUp()
        self.enterContext(frozen())

    def _compose(self):
        with projection_scope():
            domain_attention_items()
            work_queue()
            domain_dashboard_sections(cli_principal())
            dashboard_highlights(cli_principal())
            gather(*WINDOW)
            plugin_attention_items()

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

    def test_the_kit_names_a_provider_whose_answer_cannot_be_kept(self):
        manifest, contribution = sibling(identifier="example.unkept", name="Unkept")
        unkept = replace(contribution, attention=lambda: (lambda: None,))

        with installed((manifest, unkept)), self.assertLogs("severino.derivations", level="ERROR"):
            found = providers_derived_again(manifest.id)

        self.assertTrue(found)
