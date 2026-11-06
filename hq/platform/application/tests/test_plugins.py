"""Contract tests for the explicitly allowlisted plugin SDK."""

import json
import os
import re
import tempfile
from collections.abc import Iterator
from dataclasses import MISSING, asdict, fields, replace
from datetime import timedelta
from pathlib import Path
from unittest import TestCase, mock

from django.core.exceptions import ImproperlyConfigured
from django.template.loader import render_to_string
from django.test import SimpleTestCase, tag
from django.utils import timezone

from ..domains import domain_navigation
from ..pages import page_context
from ..plugin_testing import importable
from ..plugins import (
    PLUGIN_API_VERSION,
    NavigationItem,
    PluginIntegration,
    PluginManifest,
    answered_by,
    clear_plugin_composition_cache,
    describe_plugins,
    installed_integrations,
    installed_plugins,
    ordered_attention,
    plugin_capabilities,
    plugin_health,
)
from ..ui import Insight, Kpi, PageNavigation, PageSection
from .markup import hrefs, text, with_class

# What the coupling scan walks past. Generated trees, virtualenvs and vendored
# assets are not host source, and the runtime image has neither `.git` nor git
# itself, so the scan must not depend on either.
SKIP_DIRECTORIES = frozenset(
    {
        ".claude",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "backups",
        "data",
        "exports",
        "media",
        "node_modules",
        "staticfiles",
        "var",
        "venv",
    }
)
SKIP_SUFFIXES = frozenset(
    {
        ".ico",
        ".jpeg",
        ".jpg",
        ".png",
        ".pyc",
        ".pyo",
        ".sqlite3",
        ".svg",
        ".woff2",
    }
)
# Developer-local environment files: gitignored, dockerignored, and the
# supported place to record where your extension checkouts are. Naming one there
# is configuration, not a dependency in the host.
SKIP_FILE_PREFIXES = (".env", "mise.local.toml")


def _installed_extension_names() -> dict[str, str]:
    """Each name an installed extension goes by (its id, distribution, apps' and
    urlconf's packages), to what kind of name it is. The synthetic ``example.``
    namespace exists to be named, so it is left out."""

    forbidden: dict[str, str] = {}
    for plugin in installed_plugins():
        if plugin.id.startswith("example."):
            continue
        forbidden[plugin.id] = "plugin id"
        forbidden[plugin.distribution] = "distribution"
        for app in plugin.django_apps:
            forbidden[app.partition(".")[0]] = "django app"
        if plugin.urlconf:
            forbidden[plugin.urlconf.partition(".")[0]] = "urlconf package"
    return forbidden


def _host_texts(root: Path) -> Iterator[tuple[Path, str]]:
    """Each host source file under ``root`` that is text, with its body: what the coupling scan reads."""

    for directory, subdirectories, filenames in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SKIP_DIRECTORIES]
        for filename in filenames:
            path = Path(directory) / filename
            if path.suffix.lower() in SKIP_SUFFIXES or filename.startswith(SKIP_FILE_PREFIXES):
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except OSError, UnicodeDecodeError:
                continue  # binary or unreadable: it is not carrying prose
            yield path, body


VALID = PluginManifest(
    id="example.notes",
    name="Example Notes",
    version="1.2.3",
    distribution="example-notes",
    source_repository="example/example-notes",
    source_workflow=".github/workflows/admit-plugin.yml",
    api_version=PLUGIN_API_VERSION,
    integration_provider="example.plugin:integration",
    navigation=(NavigationItem("Notes", "notes:list", "notes", 40),),
    operator_capabilities=("notes.read", "notes.write"),
    mcp_read_capabilities=("notes.read",),
)


class PluginContractTests(TestCase):
    def tearDown(self):
        clear_plugin_composition_cache()

    def assertRefused(self, field, pattern=".", **overrides):
        """The manifest ``overrides`` make is refused where it is built, naming the plugin and ``field``."""

        with self.assertRaises(ImproperlyConfigured) as refused:
            replace(VALID, **overrides)
        ((named, message),) = refused.exception.violations
        self.assertEqual(named, field)
        self.assertRegex(message, pattern)
        self.assertIn(repr(overrides.get("id", VALID.id)), str(refused.exception))
        self.assertIn(field, str(refused.exception))

    def load(self, manifest=VALID):
        clear_plugin_composition_cache()
        env = mock.patch.dict(
            os.environ,
            {
                "SEVERINO_HQ_PLUGINS": "example.plugin:manifest",
                # This manifest is a fixture, so it has no signed approval and
                # cannot be in the admission lock. Stated rather than
                # inherited: admission is off under DEBUG and on otherwise, and
                # this suite also runs inside the composed image, which runs it
                # with DEBUG off.
                "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
            },
            clear=False,
        )
        importer = importable({"example.plugin": {"manifest": manifest, "integration": PluginIntegration}})
        return env, importer

    def test_empty_allowlist_is_the_secure_default(self):
        with mock.patch.dict(os.environ, {"SEVERINO_HQ_PLUGINS": ""}):
            clear_plugin_composition_cache()
            self.assertEqual(installed_plugins(), ())

    def test_the_host_source_never_names_an_installed_extension(self):
        """The host must not know which extensions exist.

        This proves it does not. Imports are checked by `hq_sdk.validation`;
        this checks the other half. A host that merely *mentions* an extension
        (in a docstring, a comment, a fixture, a default) has begun to depend on
        it, and the properties this architecture exists for start to go:
        install an extension without touching the host, run the host with none,
        release the two apart.

        The names are taken from the runtime composition rather than written
        down here, since a list of them in the host would itself be the
        coupling being tested for. So this is quiet on a checkout with no
        extensions and speaks in the composed image, where the real set exists
        and where `compose.yml` runs the suite.

        The tree is walked rather than asked of git: neither `.git` nor git
        itself exists in the runtime image, which is where this has to run.
        Walking also asks about what is installed, which is the better
        question.
        """

        forbidden = _installed_extension_names()
        if not forbidden:
            self.skipTest("no extensions composed; nothing for the host to know")

        # One alternation over each file rather than one pass per word: the
        # cost is the tree, not the tree times the extension count.
        needle = re.compile("|".join(re.escape(word) for word in sorted(forbidden)), re.IGNORECASE)
        kind_of = {word.lower(): kind for word, kind in forbidden.items()}

        root = Path(__file__).resolve().parents[4]
        offences: list[str] = []
        for path, body in _host_texts(root):
            found = needle.search(body)
            if found:
                word = found.group(0).lower()
                relative = path.relative_to(root)
                offences.append(f"{relative}: {kind_of[word]} {word!r}")

        self.assertEqual(
            sorted(offences),
            [],
            "the host names an installed extension, which is a dependency it is "
            "not supposed to have; use the synthetic example.* namespace",
        )

    def test_manifest_is_projected_into_every_registered_surface(self):
        env, importer = self.load()
        with env, importer:
            self.assertEqual(installed_plugins(), (VALID,))
            # Into the one bar the host actually renders, beside its own
            # sections, not into a plugin-only projection nothing draws.
            self.assertLessEqual(set(VALID.navigation), set(domain_navigation()))
            self.assertEqual(
                plugin_capabilities("operator"),
                frozenset({"notes.read", "notes.write"}),
            )
            self.assertEqual(plugin_capabilities("mcp_read"), frozenset({"notes.read"}))
            inventory = describe_plugins()
            self.assertEqual(inventory["schema_version"], PLUGIN_API_VERSION)
            self.assertEqual(inventory["plugins"][0]["id"], "example.notes")

    def test_incompatible_api_fails_closed(self):
        self.assertRefused("api_version", "HQ supports", api_version=999)

    def test_mcp_cannot_receive_a_capability_the_operator_does_not_hold(self):
        self.assertRefused(
            "mcp_read_capabilities",
            "operator does not hold.*notes.export",
            operator_capabilities=("notes.read",),
            mcp_read_capabilities=("notes.export",),
        )

    def test_malformed_plugin_capability_names_fail_closed(self):
        self.assertRefused("operator_capabilities", operator_capabilities=("Notes Write", "notes.read"))

    def test_a_capability_is_granted_once_per_audience(self):
        self.assertRefused("mcp_read_capabilities", "repeats", mcp_read_capabilities=("notes.read",) * 2)

    def test_a_bad_manifest_names_every_violation_at_once(self):
        with self.assertRaises(ImproperlyConfigured) as refused:
            replace(VALID, distribution="not a distribution", api_version=999, mcp_read_capabilities=("notes.export",))
        named = {field for field, _ in refused.exception.violations}
        self.assertEqual(named, {"distribution", "api_version", "mcp_read_capabilities"})
        self.assertIn(repr(VALID.id), str(refused.exception))

    def test_a_field_no_manifest_declares_is_refused(self):
        with self.assertRaisesRegex(TypeError, "mcp_capabilities"):
            PluginManifest(**{**asdict(VALID), "navigation": VALID.navigation, "mcp_capabilities": ()})

    def test_a_manifest_is_taken_as_declared_never_coerced(self):
        self.assertRefused("django_apps", django_apps=["example_notes"])
        self.assertRefused("api_version", api_version=str(PLUGIN_API_VERSION))
        with self.assertRaises(ImproperlyConfigured):
            NavigationItem("Notes", "notes:list", "notes", order=True)

    def test_a_navigation_item_names_its_namespace(self):
        self.assertRefused("navigation", "namespace", navigation=(NavigationItem("Notes", "notes:list", ""),))

    def test_a_plugin_cannot_declare_a_host_capability(self):
        """Listed under mcp_read, a host write would reach read-only agents."""

        env, importer = self.load(
            replace(
                VALID,
                operator_capabilities=("notes.read", "manage_infrastructure"),
                mcp_read_capabilities=("manage_infrastructure",),
            )
        )
        with env, importer, self.assertRaisesRegex(ImproperlyConfigured, "host capability"):
            installed_plugins()

    def test_duplicate_ids_fail_closed(self):
        clear_plugin_composition_cache()
        with (
            mock.patch.dict(
                os.environ,
                {"SEVERINO_HQ_PLUGINS": "example.one:plugin,example.two:plugin"},
            ),
            importable({"example.one": {"plugin": VALID}, "example.two": {"plugin": VALID}}),
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
        clear_plugin_composition_cache()
        with (
            mock.patch.dict(
                os.environ,
                {"SEVERINO_HQ_PLUGINS": "example.first:plugin,example.second:plugin"},
            ),
            importable({"example.first": {"plugin": first}, "example.second": {"plugin": second}}),
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
        clear_plugin_composition_cache()
        with (
            mock.patch.dict(
                os.environ,
                {"SEVERINO_HQ_PLUGINS": "example.first:plugin,example.second:plugin"},
            ),
            importable({"example.first": {"plugin": first}, "example.second": {"plugin": second}}),
            self.assertRaisesRegex(ImproperlyConfigured, "prefixes overlap"),
        ):
            installed_plugins()

    def test_every_plugin_must_declare_its_integration(self):
        self.assertRefused("integration_provider", integration_provider="")

    def test_invalid_integration_provider_reference_is_refused(self):
        self.assertRefused("integration_provider", integration_provider="not a module reference")

    def test_the_manifest_has_one_executable_integration_provider(self):
        provider_fields = [field.name for field in fields(PluginManifest) if field.name.endswith("provider")]

        self.assertEqual(provider_fields, ["integration_provider"])

    def test_plugin_api_version_is_authored_not_inherited_from_the_host(self):
        api_version = next(field for field in fields(PluginManifest) if field.name == "api_version")

        self.assertIs(api_version.default, MISSING)

    def test_plugin_registry_cannot_be_cleared_without_its_derived_graph(self):
        self.assertFalse(hasattr(installed_plugins, "cache_clear"))

    def test_integration_surfaces_stay_lazy_and_independent(self):
        dashboard = mock.Mock(return_value=())
        health = mock.Mock(return_value=True)
        contribution = PluginIntegration(dashboard=dashboard, health=health)
        factory = mock.Mock(return_value=contribution)
        env = mock.patch.dict(
            os.environ,
            {
                "SEVERINO_HQ_PLUGINS": "example.plugin:manifest",
                "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
            },
            clear=False,
        )

        with (
            env,
            importable({"example.plugin": {"manifest": VALID, "integration": factory}}),
        ):
            ((manifest, composed),) = installed_integrations()
            self.assertEqual(manifest, VALID)
            # A provider of a shared composition is asked through a derivation;
            # every other is the extension's own callable.
            self.assertIs(composed.health, health)
            self.assertEqual(answered_by(composed.dashboard), answered_by(dashboard))
            dashboard.assert_not_called()
            health.assert_not_called()
            self.assertEqual(plugin_health(), {VALID.id: True})
            health.assert_called_once_with()
            dashboard.assert_not_called()

    def test_integration_fields_fail_closed_when_not_callable(self):
        contribution = PluginIntegration(health=True)  # type: ignore[arg-type]
        env = mock.patch.dict(
            os.environ,
            {
                "SEVERINO_HQ_PLUGINS": "example.plugin:manifest",
                "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
            },
            clear=False,
        )
        with (
            env,
            importable({"example.plugin": {"manifest": VALID, "integration": lambda: contribution}}),
            self.assertRaisesRegex(ImproperlyConfigured, "must be callable: health"),
        ):
            installed_integrations()

    def test_malformed_url_prefix_is_refused(self):
        self.assertRefused("url_prefix", url_prefix="/notes", urlconf="example.urls")

    def test_route_configuration_is_atomic(self):
        self.assertRefused("urlconf", "with url_prefix", url_prefix="notes/")
        self.assertRefused("urlconf", "with url_prefix", urlconf="example.urls")

    def test_host_ui_primitives_render_for_installable_plugins(self):
        page_head = render_to_string(
            "partials/_page_head.html",
            page_context("Example Notes", "Shared host interface"),
        )
        kpis = render_to_string(
            "partials/_kpi_grid.html",
            {
                "label": "Notes summary",
                "items": (Kpi("Notes", 0, "No records yet", is_zero=True),),
            },
        )
        empty = render_to_string("partials/_empty_state.html", {"message": "No notes have been created."})
        page_navigation = render_to_string(
            "partials/_page_navigation.html",
            {"navigation": PageNavigation((PageSection("notes", "Notes"),))},
        )

        self.assertIn("Shared host interface", page_head)
        self.assertTrue(with_class(kpis, "is-zero"))
        self.assertIn("No notes have been created", text(empty))
        self.assertIn("#notes", hrefs(page_navigation))

    def test_production_plugin_without_admission_lock_fails_closed(self):
        clear_plugin_composition_cache()
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
            importable({"example.plugin": {"manifest": VALID}}),
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
            clear_plugin_composition_cache()
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
                importable({"example.plugin": {"manifest": VALID}}),
                mock.patch(
                    "hq.platform.application.plugin_admission.package_version",
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
            clear_plugin_composition_cache()
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
                importable({"example.plugin": {"manifest": VALID}}),
                self.assertRaisesRegex(ImproperlyConfigured, "version does not match"),
            ):
                installed_plugins()


@tag("source")
class PluginContractSourceTests(SimpleTestCase):
    """What the repository states about the plugin contract."""

    def test_every_statement_of_the_plugin_api_names_the_one_this_host_runs(self):
        root = Path(__file__).resolve().parents[4]
        policy = json.loads((root / "policy" / "plugin-admission-v1.json").read_text())
        self.assertEqual(policy["plugin_api_version"], PLUGIN_API_VERSION)
        for path in (*root.glob("*.md"), *root.glob("docs/*.md"), *root.glob("tests/fixtures/*/plugin.py")):
            for found in re.findall(r"api_version=(\d+)", path.read_text()):
                with self.subTest(path=path.name):
                    self.assertEqual(int(found), PLUGIN_API_VERSION)

    def test_every_host_path_the_admission_action_reads_exists(self):
        """An extension's admission checks this repository out as ``host``."""

        root = Path(__file__).resolve().parents[4]
        sources = (
            root / ".github" / "actions" / "admit-plugin" / "action.yml",
            *(root / ".github" / "workflows").glob("*.yml"),
        )
        named = {
            path
            for source in sources
            for path in re.findall(r"(?<![\w/])host/([A-Za-z0-9_./-]+\.[a-z]+)", source.read_text())
        }
        self.assertTrue(named, "the pattern found no host path, so it checks nothing")
        for path in sorted(named):
            with self.subTest(path=path):
                self.assertTrue((root / path).exists())

    def test_the_committed_inventory_stays_a_shape_and_not_a_list(self):
        """`composition/extensions.json` documents the format, never the set.

        The set is supplied at composition time through
        `COMPOSITION_EXTENSIONS`, so the host builds and runs identically with a
        different one, or with none. A committed list would make this file the
        thing that has to change to add an extension, which is the coupling the
        runtime variable exists to avoid.
        """

        root = Path(__file__).resolve().parents[4]
        document = json.loads((root / "composition" / "extensions.json").read_text(encoding="utf-8"))
        self.assertEqual(
            document["extensions"],
            [],
            "the installed set belongs in COMPOSITION_EXTENSIONS at composition time, not in the host source",
        )
        self.assertEqual(document["retired"], [], "a retired extension is named in the secret, not here")


class AttentionContractTests(TestCase):
    """The cross-surface attention queue is only useful if it is trustworthy."""

    @staticmethod
    def _manifest(**overrides):
        from hq.platform.application.plugins import PluginManifest

        base = {
            "id": "example.demo",
            "name": "Demo",
            "version": "1.0.0",
            "distribution": "demo",
            "source_repository": "owner/demo",
            "source_workflow": ".github/workflows/admit-plugin.yml",
            "api_version": PLUGIN_API_VERSION,
            "integration_provider": "demo:integration",
        }
        return PluginManifest(**{**base, **overrides})

    def _gather(self, manifests, items_by_ref):
        from hq.platform.application import plugins

        integrations = tuple(
            (
                manifest,
                PluginIntegration(
                    attention=(lambda items=tuple(items_by_ref[manifest.id]): items)
                    if manifest.id in items_by_ref
                    else None
                ),
            )
            for manifest in manifests
        )
        with mock.patch.object(plugins, "installed_integrations", return_value=integrations):
            return plugins.plugin_attention_items()

    def test_serious_items_sort_ahead_of_attention_across_extensions(self):
        from hq.platform.application.ui import Insight

        a = self._manifest(id="example.a", name="Aaa")
        b = self._manifest(id="example.b", name="Bbb")
        gathered = self._gather(
            (a, b),
            {
                "example.a": [Insight("attention", "e", "A watch", "1", "body")],
                "example.b": [Insight("serious", "e", "B urgent", "2", "body")],
            },
        )
        # Severity must beat source ordering, or the urgent item hides below.
        self.assertEqual([e["item"].title for e in gathered], ["B urgent", "A watch"])

    def test_healthy_and_neutral_items_are_not_attention(self):
        from hq.platform.application.ui import Insight

        manifest = self._manifest()
        gathered = self._gather(
            (manifest,),
            {
                "example.demo": [
                    Insight("good", "e", "Fine", "1", "body"),
                    Insight("neutral", "e", "Context", "2", "body"),
                    Insight("serious", "e", "Broken", "3", "body"),
                ]
            },
        )
        self.assertEqual([e["item"].title for e in gathered], ["Broken"])

    def test_items_carry_their_source_for_attribution(self):
        from hq.platform.application.ui import Insight

        gathered = self._gather(
            (self._manifest(),),
            {"example.demo": [Insight("serious", "e", "T", "1", "b")]},
        )
        self.assertEqual(gathered[0]["source"], "Demo")
        self.assertEqual(gathered[0]["source_id"], "example.demo")

    def test_extension_without_a_provider_is_skipped(self):
        gathered = self._gather((self._manifest(),), {})
        self.assertEqual(gathered, ())


class ComposedPluginTestKitTests(TestCase):
    """The kit itself: a plugin must be able to see a sibling in its own suite.

    Per-repo CI loads one extension. Anything that composes across them is
    otherwise only tested alone.
    """

    def tearDown(self):
        clear_plugin_composition_cache()

    def case(self, **attributes):
        from ..plugin_testing import ComposedPluginTestCase

        namespace = {"siblings": (), **attributes}
        case = type("Case", (ComposedPluginTestCase, TestCase), namespace)("run")
        case.setUp()
        self.addCleanup(case.doCleanups)
        return case

    def test_a_sibling_appears_in_the_registry(self):
        from ..plugin_testing import sibling

        self.case(siblings=(sibling(),))
        self.assertIn("example.alpha", [item.id for item in installed_plugins()])

    def test_a_sibling_contributes_dashboard_cards(self):
        from ..plugin_testing import sibling
        from ..plugins import plugin_dashboard_sections

        card = {"id": "alpha-open", "label": "Open", "value": 3, "url": "/alpha/"}
        self.case(siblings=(sibling(cards=(card,)),))

        sections = {section["id"]: section for section in plugin_dashboard_sections()}
        self.assertEqual(sections["example.alpha"]["cards"], (card,))
        self.assertEqual(sections["example.alpha"]["url"], "/alpha/")

    def test_a_sibling_contributes_a_typed_domain_overview(self):
        from ..plugin_testing import sibling
        from ..plugins import plugin_overviews
        from ..ui import DomainOverview, Kpi

        overview = DomainOverview("Current state.", "/alpha/", (Kpi("Open", 3),))
        self.case(siblings=(sibling(overview=overview),))

        sections = {section["id"]: section for section in plugin_overviews()}
        self.assertEqual(sections["example.alpha"]["overview"], overview)

    def test_a_sibling_contributes_to_the_attention_queue(self):
        from ..plugin_testing import sibling
        from ..plugins import plugin_attention_items

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
        # point of the kit.
        entries = plugin_attention_items()
        alpha = [entry for entry in entries if entry["source"] == "Alpha"]
        self.assertEqual(len(alpha), 1)
        self.assertEqual(alpha[0]["item"].title, "Something is wrong")

    def test_a_rich_domain_still_reaches_the_attention_queue(self):
        """A DomainOverview must not displace its domain's attention items.

        Overviews are a display surface and truncate, so a `serious` item must
        still reach the queue through the domain's attention items.
        """
        from ..plugin_testing import sibling
        from ..plugins import plugin_attention_items
        from ..ui import DomainOverview, Kpi

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

        from ..ui import DomainOverview

        self.assertNotIn("insights", {field.name for field in fields(DomainOverview)})

    def test_the_registry_is_restored_afterwards(self):
        from ..plugin_testing import sibling

        case = self.case(siblings=(sibling(),))
        case.doCleanups()
        self.assertNotIn("example.alpha", [item.id for item in installed_plugins()])

    def test_a_synthetic_id_cannot_displace_a_real_extension(self):
        from ..plugin_testing import sibling

        # A synthetic sibling that took a real id would quietly replace the
        # extension under test with a stub, and the suite would still pass.
        with self.assertRaises(ValueError):
            sibling(identifier="acme.records")


class AttentionOrderTests(SimpleTestCase):
    def entry(self, title, status="attention", *, since=None, notice=False, source="Example"):
        item = Insight(status, "Example", title, "", "", since=since, notice=notice)
        return {"item": item, "source": source, "source_id": "example"}

    def test_severity_then_what_needs_doing_then_the_longest_standing(self):
        now = timezone.now()
        entries = [
            self.entry("a notice", notice=True, since=now - timedelta(days=30)),
            self.entry("undated"),
            self.entry("newer", since=now - timedelta(days=1)),
            self.entry("older", since=now - timedelta(days=9), source="Zulu"),
            self.entry("serious", status="serious"),
        ]

        order = [entry["item"].title for entry in ordered_attention(entries)]

        self.assertEqual(order, ["serious", "older", "newer", "undated", "a notice"])
