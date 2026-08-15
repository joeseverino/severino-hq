"""Contract tests for the explicitly allowlisted plugin SDK."""

from __future__ import annotations

from dataclasses import replace
import os
from unittest import TestCase, mock

from django.core.exceptions import ImproperlyConfigured

from .plugins import (
    PLUGIN_API_VERSION,
    NavigationItem,
    PluginManifest,
    describe_plugins,
    installed_plugins,
    plugin_capabilities,
    plugin_navigation,
)


VALID = PluginManifest(
    id="example.notes",
    name="Example Notes",
    version="1.2.3",
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
