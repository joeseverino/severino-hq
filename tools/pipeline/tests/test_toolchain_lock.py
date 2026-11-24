"""The toolchain gate holds the images to taking their build tools from the
lock and mise from its image at min_version."""

import re

from django.test import SimpleTestCase, tag

from tools.pipeline import toolchain_lock as toolchain

DIGEST = "sha256:" + "a" * 64
BASE = f"FROM python:3.15-slim-trixie@{DIGEST} AS tools\n"
MISE = "2026.10.18"


class ImageTests(SimpleTestCase):
    def foreign(self, text):
        return toolchain.foreign_images({"Dockerfile": text}, MISE)

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


class HkSchemaTests(SimpleTestCase):
    def test_a_schema_of_another_hk_release_fails(self):
        url = "package://github.com/jdx/hk/releases/download/v{}/hk@{}#/Config.pkl"
        for text, drifts in (
            (f'amends "{url.format("2.14.0", "2.14.0")}"', False),
            (f'amends "{url.format("2.13.0", "2.13.0")}"', True),
            (f'amends "{url.format("2.14.0", "2.13.0")}"', True),
        ):
            with self.subTest(text=text):
                self.assertEqual(bool(toolchain.schema_drift(text, "2.14.0")), drifts)


@tag("source")
class PinnedTests(SimpleTestCase):
    def test_mise_renders_go_from_the_go_mod_toolchain_line(self):
        """mise.toml's Go is a template over go.mod, so this is the version the gate checks."""
        go_mod = (toolchain.ROOT / "controller" / "go.mod").read_text()
        (release,) = re.findall(r"(?m)^toolchain go(\S+)$", go_mod)
        self.assertEqual(toolchain.pinned()["go"], release)
