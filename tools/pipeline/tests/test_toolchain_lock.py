"""The toolchain gate holds mise.lock to every pinned tool, mise's Go to
go.mod, and the images to taking their build tools from the lock."""

from django.test import SimpleTestCase

from tools.pipeline import toolchain_lock as toolchain

DIGEST = "sha256:" + "a" * 64
BASE = f"FROM python:3.15-slim-trixie@{DIGEST} AS tools\n"


class ImageTests(SimpleTestCase):
    def test_stages_of_the_python_base_pass(self):
        text = BASE + "FROM tools AS build\nCOPY --from=tools /opt/bin/uv /usr/local/bin/uv\nFROM ${HQ_IMAGE}\n"
        self.assertEqual(toolchain.foreign_images({"Dockerfile": text}), [])

    def test_a_tool_taken_from_its_own_image_fails(self):
        for text in (
            f"FROM ghcr.io/astral-sh/uv:0.13.1@{DIGEST} AS uv\n" + BASE + "COPY --from=uv /uv /usr/local/bin/uv\n",
            BASE + f"COPY --from=ghcr.io/astral-sh/uv:0.13.1@{DIGEST} /uv /usr/local/bin/uv\n",
            f"FROM golang:1.27.2-trixie@{DIGEST} AS controller\n",
        ):
            with self.subTest(text=text):
                self.assertTrue(toolchain.foreign_images({"Dockerfile": text}))


class GoTests(SimpleTestCase):
    CONFIG = {"tools": {"go": "1.27.2"}}
    GO_MOD = "module example.com/controller\n\ngo 1.27\n"

    def drift(self, go_mod=GO_MOD):
        return toolchain.go_drift(self.CONFIG, go_mod)

    def test_one_release_of_the_language_version_passes(self):
        self.assertEqual(self.drift(), [])
        self.assertEqual(self.drift(go_mod=self.GO_MOD + "\ntoolchain go1.27.2\n"), [])

    def test_another_language_version_fails(self):
        for go_mod in ("go 1.26\n", "go 1.2\n", "go 1.27.1\n", "module example.com/controller\n"):
            with self.subTest(go_mod=go_mod):
                self.assertTrue(self.drift(go_mod=go_mod))

    def test_another_toolchain_fails(self):
        self.assertTrue(self.drift(go_mod=self.GO_MOD + "\ntoolchain go1.27.1\n"))


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
