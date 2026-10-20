"""An extension's identity is read from its package, and the plugin API from the host it is admitted against."""

import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from hq.platform.application.plugins import PLUGIN_API_VERSION
from tools.pipeline import plugin_identity

ROOT = Path(__file__).resolve().parents[3]


class PluginApiVersionTests(SimpleTestCase):
    def test_it_is_the_version_the_host_speaks(self):
        self.assertEqual(plugin_identity.plugin_api_version(), str(PLUGIN_API_VERSION))

    def test_a_host_that_declares_none_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            plugins = Path(directory) / plugin_identity.PLUGINS
            plugins.parent.mkdir(parents=True)
            plugins.write_text("PLUGIN_API_VERSION = compute()\n")
            with self.assertRaises(SystemExit):
                plugin_identity.plugin_api_version(Path(directory))


class PluginIdentityTests(SimpleTestCase):
    """An extension's identity is read from its package, and only from there."""

    def package(self, manifest: str, name: str = "example-alpha") -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "1.0"\n')
        source = root / "src" / "example_alpha"
        source.mkdir(parents=True)
        (source / "plugin.py").write_text(manifest)
        return root

    def manifest(self, **fields: str) -> str:
        declared = {
            "id": '"example.alpha"',
            "distribution": '"example-alpha"',
            "django_apps": '("example_alpha",)',
        } | fields
        body = ", ".join(f"{key}={value}" for key, value in declared.items())
        return f"from hq_sdk.plugin import PluginManifest\nplugin = PluginManifest({body})\n"

    def test_the_package_declares_everything_admission_needs(self):
        found = plugin_identity.identity(self.package(self.manifest()))
        self.assertEqual(
            found,
            {
                "distribution": "example-alpha",
                "plugin-id": "example.alpha",
                "plugin-reference": "example_alpha.plugin:plugin",
                "django-app": "example_alpha",
            },
        )

    def test_a_package_that_disagrees_with_itself_is_refused(self):
        cases = {
            "distribution": self.manifest(distribution='"example-beta"'),
            "django_apps": self.manifest(django_apps='("other",)'),
            "a literal": self.manifest(id="IDENTIFIER"),
            "no module-level": "plugin = make()\n",
        }
        for reason, manifest in cases.items():
            with self.subTest(reason=reason), self.assertRaises(SystemExit) as refused:
                plugin_identity.identity(self.package(manifest))
            self.assertIn(reason, str(refused.exception))

    def test_a_manifest_naming_another_repository_is_refused_where_it_is_admitted(self):
        package = self.package(self.manifest(source_repository='"owner/example-alpha"'))
        self.assertEqual(plugin_identity.identity(package, "owner/example-alpha")["plugin-id"], "example.alpha")
        with self.assertRaises(SystemExit) as refused:
            plugin_identity.identity(package, "owner/example-extensions")
        self.assertIn("owner/example-extensions", str(refused.exception))

    def test_a_manifest_away_from_its_package_is_refused(self):
        with self.assertRaises(SystemExit) as refused:
            plugin_identity.identity(self.package(self.manifest(), name="example-gamma"))
        self.assertIn("src/example_gamma/plugin.py", str(refused.exception))

    def test_an_admission_reads_the_plugin_api_beside_the_identity(self):
        printed = subprocess.run(
            [sys.executable, "-P", "-m", "tools.pipeline.plugin_identity", str(self.package(self.manifest()))],
            env={"PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        outputs = dict(line.split("=", 1) for line in printed.splitlines())
        self.assertEqual(outputs["plugin-id"], "example.alpha")
        self.assertEqual(outputs["plugin-api-version"], str(PLUGIN_API_VERSION))
