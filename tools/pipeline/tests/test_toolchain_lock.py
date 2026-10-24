"""The toolchain gate holds mise.lock to every pinned tool, and the images to
taking their build tools from the lock and mise from its image at min_version."""

import re

from django.test import SimpleTestCase, tag

from tools.pipeline import toolchain_lock as toolchain

DIGEST = "sha256:" + "a" * 64
BASE = f"FROM python:3.15-slim-trixie@{DIGEST} AS tools\n"
MISE = "2026.10.18"
PLATFORMS = ("linux-x64", "macos-arm64")


class ImageTests(SimpleTestCase):
    def foreign(self, text):
        return toolchain.foreign_images({"Dockerfile": text}, MISE)

    def test_stages_of_the_python_base_pass(self):
        text = BASE + "FROM tools AS build\nCOPY --from=tools /opt/bin/uv /usr/local/bin/uv\nFROM ${HQ_IMAGE}\n"
        self.assertEqual(self.foreign(text), [])

    def test_mise_from_its_image_at_min_version_by_digest_passes(self):
        text = BASE + f"COPY --from=ghcr.io/jdx/mise:{MISE}@{DIGEST} /usr/local/bin/mise /usr/local/bin/mise\n"
        self.assertEqual(self.foreign(text), [])

    def test_mise_from_another_release_or_without_a_digest_fails(self):
        for ref in (f"ghcr.io/jdx/mise:2026.10.4@{DIGEST}", f"ghcr.io/jdx/mise:{MISE}", f"ghcr.io/jdx/mise@{DIGEST}"):
            with self.subTest(ref=ref):
                self.assertTrue(self.foreign(f"{BASE}COPY --from={ref} /usr/local/bin/mise /usr/local/bin/mise\n"))

    def test_a_tool_taken_from_its_own_image_fails(self):
        for text in (
            f"FROM ghcr.io/astral-sh/uv:0.13.1@{DIGEST} AS uv\n" + BASE + "COPY --from=uv /uv /usr/local/bin/uv\n",
            BASE + f"COPY --from=ghcr.io/astral-sh/uv:0.13.1@{DIGEST} /uv /usr/local/bin/uv\n",
            f"FROM golang:1.27.2-trixie@{DIGEST} AS controller\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(self.foreign(text))


def locked(version, platforms=PLATFORMS):
    entry = {"version": version}
    entry.update({f"platforms.{platform}": {"checksum": DIGEST} for platform in platforms})
    return [entry]


class LockTests(SimpleTestCase):
    # Each pin at the version mise renders for it.
    TOOLS = {"node": "24.22.0", "age": "1.3.2"}

    def lock(self, **tools):
        return {"tools": {"node": locked("24.22.0"), "age": locked("1.3.2"), **tools}}

    def test_every_tool_locked_for_every_platform_passes(self):
        self.assertEqual(toolchain.missing(list(PLATFORMS), self.TOOLS, self.lock()), [])

    def test_a_tool_with_no_lock_entry_fails(self):
        for name in self.TOOLS:
            with self.subTest(tool=name):
                lock = self.lock()
                del lock["tools"][name]
                self.assertTrue(toolchain.missing(list(PLATFORMS), self.TOOLS, lock))

    def test_a_platform_without_a_checksum_fails(self):
        for platform in PLATFORMS:
            with self.subTest(platform=platform):
                others = [p for p in PLATFORMS if p != platform]
                lock = self.lock(node=locked("24.22.0", others))
                self.assertTrue(toolchain.missing(list(PLATFORMS), self.TOOLS, lock))

    def test_a_lock_of_another_version_fails(self):
        lock = self.lock(age=locked("1.3.1"))
        self.assertTrue(toolchain.missing(list(PLATFORMS), self.TOOLS, lock))


@tag("source")
class PinnedTests(SimpleTestCase):
    def test_mise_renders_go_from_the_go_mod_toolchain_line(self):
        """mise.toml's Go is a template over go.mod, so this is the version the gate checks."""
        go_mod = (toolchain.ROOT / "controller" / "go.mod").read_text()
        (release,) = re.findall(r"(?m)^toolchain go(\S+)$", go_mod)
        self.assertEqual(toolchain.pinned()["go"], release)
