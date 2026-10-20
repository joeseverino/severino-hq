"""The toolchain gate holds mise.lock to every pinned tool, and the images'
uv to the version mise pins."""

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


def locked(version, platforms=toolchain.PLATFORMS):
    entry = {"version": version}
    entry.update({f"platforms.{platform}": {"checksum": DIGEST} for platform in platforms})
    return [entry]


class LockTests(SimpleTestCase):
    # Node is pinned like any other tool; a lazy one is declared as a table.
    TOOLS = {"node": "24.22.0", "age": {"version": "1.3.2", "lazy": True}}

    def lock(self, **tools):
        return {"tools": {"node": locked("24.22.0"), "age": locked("1.3.2"), **tools}}

    def test_every_tool_locked_for_every_platform_passes(self):
        self.assertEqual(toolchain.missing({"tools": self.TOOLS}, self.lock()), [])

    def test_a_tool_with_no_lock_entry_fails(self):
        for name in self.TOOLS:
            with self.subTest(tool=name):
                lock = self.lock()
                del lock["tools"][name]
                self.assertTrue(toolchain.missing({"tools": self.TOOLS}, lock))

    def test_a_platform_without_a_checksum_fails(self):
        for platform in toolchain.PLATFORMS:
            with self.subTest(platform=platform):
                others = [p for p in toolchain.PLATFORMS if p != platform]
                lock = self.lock(node=locked("24.22.0", others))
                self.assertTrue(toolchain.missing({"tools": self.TOOLS}, lock))

    def test_a_lock_of_another_version_fails(self):
        lock = self.lock(age=locked("1.3.1"))
        self.assertTrue(toolchain.missing({"tools": self.TOOLS}, lock))
