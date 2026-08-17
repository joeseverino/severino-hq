"""Contract tests for the explicitly allowlisted plugin SDK."""

from __future__ import annotations

from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
from unittest import TestCase, mock

from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string

from .plugins import (
    PLUGIN_API_VERSION,
    NavigationItem,
    PluginManifest,
    describe_plugins,
    installed_plugins,
    plugin_capabilities,
    plugin_navigation,
    plugin_token_authenticated_prefixes,
)
from .ui import Insight, Kpi


VALID = PluginManifest(
    id="example.notes",
    name="Example Notes",
    version="1.2.3",
    distribution="example-notes",
    source_repository="example/example-notes",
    source_workflow=".github/workflows/admit-plugin.yml",
    navigation=(NavigationItem("Notes", "notes:list", "notes", 40),),
    operator_capabilities=("notes.read", "notes.write"),
    mcp_read_capabilities=("notes.read",),
)


class PluginContractTests(TestCase):
    def tearDown(self):
        installed_plugins.cache_clear()

    def load(self, manifest=VALID):
        installed_plugins.cache_clear()
        env = mock.patch.dict(
            os.environ,
            {"SEVERINO_HQ_PLUGINS": "example.plugin:manifest"},
            clear=False,
        )
        importer = mock.patch("application.plugins._import", return_value=manifest)
        return env, importer

    def test_empty_allowlist_is_the_secure_default(self):
        with mock.patch.dict(os.environ, {"SEVERINO_HQ_PLUGINS": ""}):
            installed_plugins.cache_clear()
            self.assertEqual(installed_plugins(), ())

    def test_the_public_repo_never_commits_the_private_composition_inventory(self):
        root = Path(__file__).resolve().parents[1]
        document = json.loads(
            (root / "composition" / "extensions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            document["extensions"],
            [],
            "private extension identities belong in COMPOSITION_EXTENSIONS, "
            "never in the public host repository",
        )

    def test_manifest_is_projected_into_every_registered_surface(self):
        env, importer = self.load()
        with env, importer:
            self.assertEqual(installed_plugins(), (VALID,))
            self.assertEqual(plugin_navigation(), VALID.navigation)
            self.assertEqual(
                plugin_capabilities("operator"),
                frozenset({"notes.read", "notes.write"}),
            )
            self.assertEqual(plugin_capabilities("mcp_read"), frozenset({"notes.read"}))
            inventory = describe_plugins()
            self.assertEqual(inventory["schema_version"], PLUGIN_API_VERSION)
            self.assertEqual(inventory["plugins"][0]["id"], "example.notes")

    def test_incompatible_api_fails_closed(self):
        env, importer = self.load(replace(VALID, api_version=999))
        with env, importer, self.assertRaises(ImproperlyConfigured):
            installed_plugins()

    def test_mcp_cannot_receive_a_capability_the_operator_does_not_hold(self):
        env, importer = self.load(
            replace(
                VALID,
                operator_capabilities=("notes.read",),
                mcp_read_capabilities=("notes.export",),
            )
        )
        with env, importer, self.assertRaisesRegex(
            ImproperlyConfigured, "operator does not hold"
        ):
            installed_plugins()

    def test_malformed_plugin_capability_names_fail_closed(self):
        env, importer = self.load(
            replace(VALID, operator_capabilities=("Notes Write",))
        )
        with env, importer, self.assertRaisesRegex(
            ImproperlyConfigured, "invalid capability"
        ):
            installed_plugins()

    def test_duplicate_ids_fail_closed(self):
        installed_plugins.cache_clear()
        with (
            mock.patch.dict(
                os.environ,
                {"SEVERINO_HQ_PLUGINS": "one:plugin,two:plugin"},
            ),
            mock.patch("application.plugins._import", return_value=VALID),
            self.assertRaises(ImproperlyConfigured),
        ):
            installed_plugins()

    def test_duplicate_plugin_mounts_fail_before_django_orders_them(self):
        first = replace(
            VALID,
            id="example.first",
            distribution="example-first",
            url_prefix="records/",
            urlconf="example_first.urls",
        )
        second = replace(
            VALID,
            id="example.second",
            distribution="example-second",
            url_prefix="records/",
            urlconf="example_second.urls",
        )
        installed_plugins.cache_clear()
        with (
            mock.patch.dict(
                os.environ,
                {"SEVERINO_HQ_PLUGINS": "first:plugin,second:plugin"},
            ),
            mock.patch(
                "application.plugins._import",
                side_effect=(first, second),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "Duplicate plugin URL prefix"),
        ):
            installed_plugins()

    def test_nested_plugin_mounts_fail_instead_of_shadowing_by_order(self):
        first = replace(
            VALID,
            id="example.first",
            distribution="example-first",
            url_prefix="records/",
            urlconf="example_first.urls",
        )
        second = replace(
            VALID,
            id="example.second",
            distribution="example-second",
            url_prefix="records/archive/",
            urlconf="example_second.urls",
        )
        installed_plugins.cache_clear()
        with (
            mock.patch.dict(
                os.environ,
                {"SEVERINO_HQ_PLUGINS": "first:plugin,second:plugin"},
            ),
            mock.patch(
                "application.plugins._import",
                side_effect=(first, second),
            ),
            self.assertRaisesRegex(ImproperlyConfigured, "prefixes overlap"),
        ):
            installed_plugins()

    def test_invalid_provider_reference_fails_at_startup(self):
        env, importer = self.load(
            replace(VALID, health_provider="not a module reference")
        )
        with env, importer, self.assertRaisesRegex(
            ImproperlyConfigured, "invalid health_provider"
        ):
            installed_plugins()

    def test_malformed_url_prefix_fails_at_startup(self):
        env, importer = self.load(
            replace(VALID, url_prefix="/notes", urlconf="example.urls")
        )
        with env, importer, self.assertRaisesRegex(
            ImproperlyConfigured, "invalid url_prefix"
        ):
            installed_plugins()

    def test_route_configuration_is_atomic(self):
        env, importer = self.load(replace(VALID, url_prefix="notes/"))
        with env, importer, self.assertRaises(ImproperlyConfigured):
            installed_plugins()

    def test_token_authenticated_routes_are_anchored_to_the_plugin_mount(self):
        env, importer = self.load(
            replace(
                VALID,
                url_prefix="mobile/",
                urlconf="example.urls",
                token_authenticated_routes=("api/v1/",),
            )
        )
        with env, importer:
            self.assertEqual(
                plugin_token_authenticated_prefixes(), ("/mobile/api/v1/",)
            )

    def test_a_token_route_cannot_escape_its_own_mount(self):
        for route in ("/admin/", "../admin/", ""):
            with self.subTest(route=route):
                env, importer = self.load(
                    replace(
                        VALID,
                        url_prefix="mobile/",
                        urlconf="example.urls",
                        token_authenticated_routes=(route,),
                    )
                )
                with env, importer, self.assertRaises(ImproperlyConfigured):
                    installed_plugins()

    def test_a_token_route_without_a_mount_fails_closed(self):
        env, importer = self.load(
            replace(VALID, token_authenticated_routes=("api/v1/",))
        )
        with env, importer, self.assertRaises(ImproperlyConfigured):
            installed_plugins()

    def test_host_ui_primitives_render_for_installable_plugins(self):
        page_head = render_to_string(
            "partials/_page_head.html",
            {"title": "Example Notes", "lede": "Shared host interface"},
        )
        kpis = render_to_string(
            "partials/_kpi_grid.html",
            {
                "label": "Notes summary",
                "items": (Kpi("Notes", 0, "No records yet", is_zero=True),),
            },
        )
        empty = render_to_string(
            "partials/_empty_state.html", {"message": "No notes have been created."}
        )

        self.assertIn("Shared host interface", page_head)
        self.assertIn('class="kpi is-zero"', kpis)
        self.assertIn("No notes have been created", empty)

    def test_production_plugin_without_admission_lock_fails_closed(self):
        installed_plugins.cache_clear()
        with (
            mock.patch.dict(
                os.environ,
                {
                    "DJANGO_DEBUG": "0",
                    "SEVERINO_HQ_PLUGINS": "example.plugin:manifest",
                    "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "1",
                    "SEVERINO_HQ_PLUGIN_POLICY_SHA256": "c" * 64,
                },
                clear=True,
            ),
            mock.patch("application.plugins._import", return_value=VALID),
            self.assertRaisesRegex(ImproperlyConfigured, "PLUGIN_LOCK is required"),
        ):
            installed_plugins()

    def test_exact_cordon_admission_allows_installed_distribution(self):
        policy = "c" * 64
        approval = {
            "ok": True,
            "schema_version": 1,
            "plugin": VALID.id,
            "version": VALID.version,
            "distribution": VALID.distribution,
            "host": "severino-hq",
            "plugin_api_version": PLUGIN_API_VERSION,
            "source_repository": VALID.source_repository,
            "source_workflow": VALID.source_workflow,
            "source_commit": "b" * 40,
            "signer_identity": "https://github.com/example/example-notes/.github/workflows/admit-plugin.yml@refs/heads/main",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "artifact_sha256": "a" * 64,
            "policy_sha256": policy,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as lock:
            json.dump(
                {
                    "schema_version": 1,
                    "host": "severino-hq",
                    "plugins": [approval],
                },
                lock,
            )
            lock.flush()
            installed_plugins.cache_clear()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "DJANGO_DEBUG": "0",
                        "SEVERINO_HQ_PLUGINS": "example.plugin:manifest",
                        "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "1",
                        "SEVERINO_HQ_PLUGIN_LOCK": lock.name,
                        "SEVERINO_HQ_PLUGIN_POLICY_SHA256": policy,
                    },
                    clear=True,
                ),
                mock.patch("application.plugins._import", return_value=VALID),
                mock.patch(
                    "application.plugin_admission.package_version",
                    return_value=VALID.version,
                ),
            ):
                self.assertEqual(installed_plugins(), (VALID,))

    def test_admission_version_mismatch_fails_closed(self):
        policy = "c" * 64
        approval = {
            "ok": True,
            "schema_version": 1,
            "plugin": VALID.id,
            "version": "9.9.9",
            "distribution": VALID.distribution,
            "host": "severino-hq",
            "plugin_api_version": PLUGIN_API_VERSION,
            "source_repository": VALID.source_repository,
            "source_workflow": VALID.source_workflow,
            "source_commit": "b" * 40,
            "signer_identity": "https://github.com/example/example-notes/.github/workflows/admit-plugin.yml@refs/heads/main",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "artifact_sha256": "a" * 64,
            "policy_sha256": policy,
        }
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json") as lock:
            json.dump(
                {
                    "schema_version": 1,
                    "host": "severino-hq",
                    "plugins": [approval],
                },
                lock,
            )
            lock.flush()
            installed_plugins.cache_clear()
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "DJANGO_DEBUG": "0",
                        "SEVERINO_HQ_PLUGINS": "example.plugin:manifest",
                        "SEVERINO_HQ_PLUGIN_LOCK": lock.name,
                        "SEVERINO_HQ_PLUGIN_POLICY_SHA256": policy,
                    },
                    clear=True,
                ),
                mock.patch("application.plugins._import", return_value=VALID),
                self.assertRaisesRegex(ImproperlyConfigured, "version does not match"),
            ):
                installed_plugins()


class AttentionContractTests(TestCase):
    """The cross-surface attention queue is only useful if it is trustworthy."""

    @staticmethod
    def _manifest(**overrides):
        from application.plugins import PluginManifest

        base = dict(
            id="example.demo",
            name="Demo",
            version="1.0.0",
            distribution="demo",
            source_repository="owner/demo",
            source_workflow=".github/workflows/admit-plugin.yml",
            attention_provider="demo:items",
        )
        return PluginManifest(**{**base, **overrides})

    def _gather(self, manifests, items_by_ref):
        from application import plugins

        with (
            mock.patch.object(plugins, "installed_plugins", return_value=manifests),
            mock.patch.object(
                plugins, "_import", side_effect=lambda ref: (lambda: items_by_ref[ref])
            ),
        ):
            return plugins.plugin_attention_items()

    def test_serious_items_sort_ahead_of_attention_across_extensions(self):
        from application.ui import Insight

        a = self._manifest(id="example.a", name="Aaa", attention_provider="a:items")
        b = self._manifest(id="example.b", name="Bbb", attention_provider="b:items")
        gathered = self._gather(
            (a, b),
            {
                "a:items": [Insight("attention", "e", "A watch", "1", "body")],
                "b:items": [Insight("serious", "e", "B urgent", "2", "body")],
            },
        )
        # Severity must beat source ordering, or the urgent item hides below.
        self.assertEqual([e["item"].title for e in gathered], ["B urgent", "A watch"])

    def test_healthy_and_neutral_items_are_not_attention(self):
        from application.ui import Insight

        manifest = self._manifest()
        gathered = self._gather(
            (manifest,),
            {
                "demo:items": [
                    Insight("good", "e", "Fine", "1", "body"),
                    Insight("neutral", "e", "Context", "2", "body"),
                    Insight("serious", "e", "Broken", "3", "body"),
                ]
            },
        )
        self.assertEqual([e["item"].title for e in gathered], ["Broken"])

    def test_items_carry_their_source_for_attribution(self):
        from application.ui import Insight

        gathered = self._gather(
            (self._manifest(),), {"demo:items": [Insight("serious", "e", "T", "1", "b")]}
        )
        self.assertEqual(gathered[0]["source"], "Demo")
        self.assertEqual(gathered[0]["source_id"], "example.demo")

    def test_extension_without_a_provider_is_skipped(self):
        gathered = self._gather((self._manifest(attention_provider=""),), {})
        self.assertEqual(gathered, ())


class ComposedPluginTestKitTests(TestCase):
    """The kit itself: a plugin must be able to see a sibling in its own suite.

    Per-repo CI loads one extension. Anything that composes across them is
    therefore only tested alone, which is how an assertion that nothing else is
    installed passes locally and is wrong in production.
    """

    def tearDown(self):
        installed_plugins.cache_clear()

    def case(self, **attributes):
        from .plugin_testing import ComposedPluginTestCase

        namespace = {"siblings": (), **attributes}
        case = type("Case", (ComposedPluginTestCase, TestCase), namespace)("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_a_sibling_appears_in_the_registry(self):
        from .plugin_testing import sibling

        self.case(siblings=(sibling(),))
        self.assertIn("example.alpha", [item.id for item in installed_plugins()])

    def test_a_sibling_contributes_dashboard_cards(self):
        from .plugin_testing import sibling
        from .plugins import plugin_dashboard_sections

        card = {"id": "alpha-open", "label": "Open", "value": 3, "url": "/alpha/"}
        self.case(siblings=(sibling(cards=(card,)),))

        sections = {section["id"]: section for section in plugin_dashboard_sections()}
        self.assertEqual(sections["example.alpha"]["cards"], (card,))
        self.assertEqual(sections["example.alpha"]["url"], "/alpha/")

    def test_a_sibling_contributes_a_typed_domain_overview(self):
        from .plugin_testing import sibling
        from .plugins import plugin_overviews
        from .ui import DomainOverview, Kpi

        overview = DomainOverview("Current state.", "/alpha/", (Kpi("Open", 3),))
        self.case(siblings=(sibling(overview=overview),))

        sections = {section["id"]: section for section in plugin_overviews()}
        self.assertEqual(sections["example.alpha"]["overview"], overview)

    def test_a_sibling_contributes_to_the_attention_queue(self):
        from .plugin_testing import sibling
        from .plugins import plugin_attention_items

        item = Insight(
            status="serious",
            eyebrow="Alpha",
            title="Something is wrong",
            value="1",
            body="Body.",
        )
        self.case(siblings=(sibling(attention=(item,)),))

        # Asserts the sibling is present, not that it is alone: this suite runs
        # with whatever extension is installed alongside, which is the whole
        # point of the kit and was the exact assumption it exists to catch.
        entries = plugin_attention_items()
        alpha = [entry for entry in entries if entry["source"] == "Alpha"]
        self.assertEqual(len(alpha), 1)
        self.assertEqual(alpha[0]["item"].title, "Something is wrong")

    def test_a_rich_domain_still_reaches_the_attention_queue(self):
        """A DomainOverview must not displace its domain's attention items.

        The regression: a composing surface dropped every domain that supplied
        an overview from the queue and re-read severity off the overview
        instead. Overviews are a display surface and truncate, so a `serious`
        item past the cutoff vanished from the one place it was guaranteed to
        appear -- and the page looked healthier the more a domain reported.
        """
        from .plugin_testing import sibling
        from .plugins import plugin_attention_items
        from .ui import DomainOverview, Kpi

        item = Insight(
            status="serious",
            eyebrow="Alpha",
            title="Still wrong",
            value="1",
            body="Body.",
        )
        self.case(
            siblings=(
                sibling(
                    attention=(item,),
                    overview=DomainOverview("State.", "/alpha/", (Kpi("Open", 3),)),
                ),
            )
        )

        titles = [entry["item"].title for entry in plugin_attention_items()]
        self.assertIn("Still wrong", titles)

    def test_an_overview_cannot_carry_attention_items(self):
        """One channel for "needs a decision", enforced by the type."""
        from dataclasses import fields

        from .ui import DomainOverview

        self.assertNotIn("insights", {field.name for field in fields(DomainOverview)})

    def test_the_registry_is_restored_afterwards(self):
        from .plugin_testing import sibling

        case = self.case(siblings=(sibling(),))
        case.doCleanups()
        self.assertNotIn("example.alpha", [item.id for item in installed_plugins()])

    def test_a_synthetic_id_cannot_displace_a_real_extension(self):
        from .plugin_testing import sibling

        # A synthetic sibling that took a real id would quietly replace the
        # extension under test with a stub, and the suite would still pass.
        with self.assertRaises(ValueError):
            sibling(identifier="acme.records")
