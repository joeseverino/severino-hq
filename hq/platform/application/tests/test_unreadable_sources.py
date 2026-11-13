"""An extension composing its siblings through the SDK reads a failing one as unreadable, and the rest as they are."""

from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase, TestCase

from .. import plugins
from ..card_items import Insight, Kpi
from ..plugin_testing import installed, providers_derived_again, sibling
from ..plugins import PLUGIN_API_VERSION, PluginIntegration, PluginManifest
from ..ui import DomainOverview

LOGGER = "severino.composition"
EVENT = "composition.source.unreadable"
GOOD_CARD = {"id": "example-good-1", "label": "Good reading", "value": 1, "url": "/"}
GOOD_ITEM = Insight("serious", "Example", "Good source item", "1", "Look into it.")


def broken():
    raise RuntimeError("stored readings no longer parse")


def manifest(name, **fields):
    return PluginManifest(
        id=f"example.{name}",
        name=f"Example {name}",
        version="1.0.0",
        distribution=f"example-{name}",
        source_repository="example/example-extensions",
        source_workflow=".github/workflows/admit-plugin.yml",
        api_version=PLUGIN_API_VERSION,
        integration_provider=f"example.{name}:integration",
        **fields,
    )


class UnreadableSiblingTests(SimpleTestCase):
    def installed(self, good, failing, **failing_fields):
        integrations = ((manifest("good"), good), (manifest("broken", **failing_fields), failing))
        patcher = mock.patch.object(plugins, "installed_integrations", return_value=integrations)
        patcher.start()
        self.addCleanup(patcher.stop)

    def assertLogsTheFailure(self, logs):
        (record,) = [record for record in logs.records if getattr(record, "event", None) == EVENT]
        self.assertIn("example.broken", record.getMessage())
        self.assertIsNotNone(record.exc_info)

    def attention(self, failing):
        self.installed(PluginIntegration(attention=lambda: (GOOD_ITEM,)), PluginIntegration(attention=failing))
        with self.assertLogs(LOGGER, "ERROR") as logs:
            gathered = plugins.plugin_attention_items()
        self.assertLogsTheFailure(logs)
        return {entry["source_id"]: entry["item"] for entry in gathered}

    def cards(self, failing):
        self.installed(PluginIntegration(dashboard=lambda: (GOOD_CARD,)), PluginIntegration(dashboard=failing))
        with self.assertLogs(LOGGER, "ERROR") as logs:
            sections = {section["id"]: section["cards"] for section in plugins.plugin_dashboard_sections()}
        self.assertLogsTheFailure(logs)
        self.assertEqual(sections["example.good"], (GOOD_CARD,))
        (unreadable,) = sections["example.broken"]
        return unreadable

    def overviews(self, failing):
        overview = DomainOverview("Example", "/example/", (Kpi("Good reading", 1),))
        self.installed(PluginIntegration(overview=lambda: overview), PluginIntegration(overview=failing))
        with self.assertLogs(LOGGER, "ERROR") as logs:
            overviews = plugins.plugin_overviews()
        self.assertLogsTheFailure(logs)
        self.assertEqual([(entry["id"], entry["overview"]) for entry in overviews], [("example.good", overview)])

    def test_attention_items_carry_an_unreadable_sibling_as_one_item(self):
        by_source = self.attention(broken)

        self.assertEqual(by_source["example.good"], GOOD_ITEM)
        self.assertEqual(by_source["example.broken"].status, "attention")
        self.assertEqual(by_source["example.broken"].title, "Example broken could not be read")

    def test_an_item_with_a_status_outside_the_vocabulary_is_its_sources_failure(self):
        by_source = self.attention(lambda: (SimpleNamespace(status="urgent"),))

        self.assertEqual(by_source["example.good"], GOOD_ITEM)
        self.assertEqual(by_source["example.broken"].title, "Example broken could not be read")

    def test_the_stand_in_is_gated_on_what_a_reader_of_its_extension_holds(self):
        self.installed(
            PluginIntegration(attention=lambda: (GOOD_ITEM,)),
            PluginIntegration(attention=broken),
            operator_capabilities=("example.broken.read",),
            mcp_read_capabilities=("example.broken.read",),
        )
        with self.assertLogs(LOGGER, "ERROR"):
            by_source = {entry["source_id"]: entry["item"] for entry in plugins.plugin_attention_items()}

        self.assertEqual(by_source["example.broken"].read_capabilities, ("example.broken.read",))

    def test_two_items_under_one_key_are_one_the_worst_and_the_rest_of_the_source_stays(self):
        # Two certificates for one name, read under one key: the queue knows an
        # item by its key, so it keeps the worse and says why the other went.
        later = Insight("attention", "Example", "Lapses in ten days", "10", "", key="certificate:example.com")
        sooner = replace(later, status="serious", title="Lapsed")
        other = Insight("attention", "Example", "Something else", "1", "", key="other")
        self.installed(PluginIntegration(), PluginIntegration(attention=lambda: (later, other, sooner)))

        with self.assertLogs(LOGGER, "ERROR") as logs:
            gathered = [entry["item"] for entry in plugins.plugin_attention_items()]

        self.assertEqual(sorted(item.title for item in gathered), ["Lapsed", "Something else"])
        (record,) = logs.records
        self.assertIn("example.broken", record.getMessage())
        self.assertIn("example.broken:certificate:example.com", record.getMessage())

    def test_dashboard_sections_carry_an_unreadable_sibling_as_one_card(self):
        unreadable = self.cards(broken)

        self.assertEqual(unreadable["value"], "Unreadable")
        self.assertEqual(unreadable["status"], "attention")

    def test_a_card_with_a_key_the_host_does_not_know_is_its_sources_failure(self):
        unreadable = self.cards(lambda: ({**GOOD_CARD, "id": "example-broken-1", "tint": "red"},))

        self.assertEqual(unreadable["value"], "Unreadable")

    def test_two_cards_of_one_source_sharing_an_id_are_its_sources_failure(self):
        card = {**GOOD_CARD, "id": "example-broken-1"}
        unreadable = self.cards(lambda: (card, card))

        self.assertEqual(unreadable["value"], "Unreadable")

    def test_overviews_leave_out_an_unreadable_sibling(self):
        self.overviews(broken)

    def test_an_overview_of_another_shape_is_its_sources_failure(self):
        self.overviews(lambda: {"description": "Example"})

    def test_an_overview_that_answers_nothing_is_no_section(self):
        self.installed(PluginIntegration(), PluginIntegration(overview=lambda: None))

        self.assertEqual(plugins.plugin_overviews(), ())


class KitNamesARefusedAnswerTests(TestCase):
    def test_the_kit_names_each_provider_whose_answer_the_host_refuses(self):
        manifest, contribution = sibling(identifier="example.malformed", name="Malformed")
        malformed = replace(
            contribution,
            attention=lambda: (SimpleNamespace(status="urgent"),),
            dashboard=lambda: ({**GOOD_CARD, "tint": "red"},),
            overview=lambda: {"description": "Example"},
        )

        with installed((manifest, malformed)):
            found = providers_derived_again(manifest.id)

        self.assertEqual({line.partition(":")[0] for line in found}, {"attention", "dashboard", "overview"})

    def test_the_kit_names_a_provider_giving_two_items_one_key(self):
        manifest, contribution = sibling(identifier="example.repeated", name="Repeated")
        repeated = replace(contribution, attention=lambda: (GOOD_ITEM, replace(GOOD_ITEM, value="2")))

        with installed((manifest, repeated)):
            found = providers_derived_again(manifest.id)

        self.assertEqual([line.partition(":")[0] for line in found], ["attention"])

    def test_the_kit_names_nothing_of_a_well_formed_provider(self):
        overview = DomainOverview("Example", "/example/", (Kpi("Good reading", 1),))
        manifest, contribution = sibling(
            identifier="example.wellformed",
            name="Well formed",
            cards=(GOOD_CARD,),
            attention=(GOOD_ITEM,),
            overview=overview,
        )

        with installed((manifest, contribution)):
            self.assertEqual(providers_derived_again(manifest.id), [])
