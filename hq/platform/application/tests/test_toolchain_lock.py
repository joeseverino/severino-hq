"""The toolchain gate holds the images' uv to the version mise pins."""

import importlib.util
from pathlib import Path

from django.test import SimpleTestCase

ROOT = Path(__file__).resolve().parents[4]
spec = importlib.util.spec_from_file_location("toolchain_lock", ROOT / "scripts/toolchain_lock.py")
assert spec and spec.loader
toolchain = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toolchain)

DIGEST = "sha256:" + "a" * 64
CONFIG = {"tools": {"uv": "0.13.1"}}


class UvImageTests(SimpleTestCase):
    def test_an_image_at_the_pinned_version_passes(self):
        images = {"Dockerfile": f"FROM ghcr.io/astral-sh/uv:0.13.1@{DIGEST} AS uv\n"}
        self.assertEqual(toolchain.image_drift(CONFIG, images), [])

    def test_an_image_at_another_version_fails(self):
        images = {"Dockerfile": f"FROM ghcr.io/astral-sh/uv:0.12.0@{DIGEST} AS uv\n"}
        self.assertTrue(toolchain.image_drift(CONFIG, images))

    def test_an_image_without_a_digest_pinned_uv_fails(self):
        for text in ("FROM ghcr.io/astral-sh/uv:0.13.1 AS uv\n", "FROM python:3.15\n"):
            with self.subTest(text=text):
                self.assertTrue(toolchain.image_drift(CONFIG, {"Dockerfile": text}))
