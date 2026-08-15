"""Contract tests for the explicitly allowlisted plugin SDK."""

from __future__ import annotations

from dataclasses import replace
import json
import os
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
)
from .ui import Kpi


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

    def test_route_configuration_is_atomic(self):
        env, importer = self.load(replace(VALID, url_prefix="notes/"))
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
