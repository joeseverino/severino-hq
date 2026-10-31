"""setup:extensions names the set Compose would build from the same checkouts."""

import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from tools.dev.extension_set import extension_set

MANIFEST = (
    'plugin = PluginManifest(id="example.{name}", distribution="example-{name}", django_apps=("example_{name}",))\n'
)


class ExtensionSetTests(SimpleTestCase):
    def checkout(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def package(self, root: Path, name: str) -> Path:
        (root / "src" / f"example_{name}").mkdir(parents=True)
        (root / "pyproject.toml").write_text(f'[project]\nname = "example-{name}"\n')
        (root / "src" / f"example_{name}" / "plugin.py").write_text(MANIFEST.format(name=name))
        return root

    def test_every_package_of_a_repository_of_them_is_named(self):
        repository = self.checkout()
        (repository / "pyproject.toml").write_text("[tool.uv.workspace]\n")
        beta = self.package(repository / "packages" / "beta", "beta")
        alpha = self.package(repository / "packages" / "alpha", "alpha")
        single = self.package(self.checkout(), "gamma")
        named = extension_set([repository, single])
        self.assertEqual(
            named["HQ_LOCAL_PLUGINS"],
            "example_alpha.plugin:plugin,example_beta.plugin:plugin,example_gamma.plugin:plugin",
        )
        for root in (alpha, beta, single):
            self.assertIn(str(root.resolve() / "src"), named["HQ_LOCAL_PYTHONPATH"])
        self.assertIn("get_env(name='HQ_LOCAL_PYTHONPATH'", named["HQ_LOCAL_PYTHONPATH"])

    def test_a_checkout_with_no_package_is_refused(self):
        with self.assertRaises(SystemExit):
            extension_set([self.checkout()])

    def test_a_package_whose_manifest_disagrees_with_it_is_refused(self):
        root = self.package(self.checkout(), "alpha")
        (root / "pyproject.toml").write_text('[project]\nname = "example-other"\n')
        with self.assertRaises(SystemExit):
            extension_set([root])
