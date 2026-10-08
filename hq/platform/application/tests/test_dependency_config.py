"""Dependency bootstrap and config-reader contracts."""

import importlib.util
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("dependency_config", ROOT / "scripts/dependency_config.py")
assert spec and spec.loader
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class DependencyConfigTests(SimpleTestCase):
    def test_bootstrap_uses_the_declared_pin_and_lock_hashes(self):
        version = config.tool_pin("uv")
        requirement = config.uv_requirements()
        self.assertTrue(requirement.startswith(f"uv=={version} --hash=sha256:"))
        self.assertNotIn(" @ ", requirement)

    def test_missing_or_duplicate_exact_pin_fails(self):
        for declarations in (["uv>=0.12"], ["uv==0.12", "uv==0.13"]):
            with self.subTest(declarations=declarations), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "pyproject.toml").write_text(
                    "[dependency-groups]\ntools = " + json.dumps(declarations) + "\nbrowser = []\n"
                )
                with self.assertRaisesRegex(ValueError, "one exact tool pin"):
                    config.tool_pin("uv", root)

    def test_bootstrap_refuses_unhashed_or_nonregistry_artifacts(self):
        for source, hashes in (
            ("{path = '.'}", '[{hash = "sha256:' + "a" * 64 + '"}]'),
            ('{registry = "https://pypi.org/simple"}', "[]"),
        ):
            with self.subTest(source=source, hashes=hashes), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "pyproject.toml").write_text('[dependency-groups]\ntools = ["uv==0.12"]\nbrowser = []\n')
                (root / "uv.lock").write_text(
                    '[[package]]\nname="uv"\nversion="0.12"\nsource=' + source + "\nwheels=" + hashes + "\n"
                )
                with self.assertRaises(ValueError):
                    config.uv_requirements(root)
