"""lint:sdk-breaking fails a break of hq_sdk's shape that PLUGIN_API_VERSION does not move with, and
names what broke; an addition, or a break the version moves with, passes."""

import tempfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from tools.gate.tests.base import fixture_git

PLUGINS = "hq/platform/application/plugins.py"
SHAPES = """\
from dataclasses import dataclass
from typing import dataclass_transform


@dataclass_transform(frozen_default=True)
def display(cls):
    return dataclass(frozen=True)(cls)


@display
class Card:
    title: str = ""
    detail: str = ""


def lookup(name: str, *, limit: int = 10) -> str:
    return name
"""
SDK = """\
from hq.platform.application.shapes import Card, lookup

__all__ = ["Card", "lookup"]
"""


class VerdictTests(SimpleTestCase):
    """Griffe judges a planted host and SDK in a fixture repository, as the gate runs it on the real one."""

    def judge(self, *, shapes: str = SHAPES, sdk: str = SDK, version: int = 1) -> tuple[int, str]:
        from tools import sdk_breaking

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        files = {
            "hq/__init__.py": "",
            "hq/platform/__init__.py": "",
            "hq/platform/application/__init__.py": "",
            PLUGINS: "PLUGIN_API_VERSION = 1\n",
            "hq/platform/application/shapes.py": SHAPES,
            "hq_sdk/__init__.py": "",
            "hq_sdk/example.py": SDK,
        }
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        git = fixture_git(root)
        git("init", "-q")
        git("add", ".")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD")
        (root / "hq/platform/application/shapes.py").write_text(shapes)
        (root / "hq_sdk/example.py").write_text(sdk)
        (root / PLUGINS).write_text(f"PLUGIN_API_VERSION = {version}\n")
        said = StringIO()
        with (
            patch.object(sdk_breaking, "ROOT", root),
            patch.object(sdk_breaking, "merge_base", return_value=base),
            redirect_stdout(said),
            redirect_stderr(said),
        ):
            code = sdk_breaking.main([])
        return code, said.getvalue()

    def test_a_break_without_the_version_fails_and_names_what_broke(self):
        code, said = self.judge(shapes=SHAPES.replace(", *, limit: int = 10", ""))

        self.assertEqual(code, 1)
        self.assertIn("lookup(limit)", said)
        self.assertIn("Parameter was removed", said)

    def test_the_same_break_with_the_version_moved_passes(self):
        code, said = self.judge(shapes=SHAPES.replace(", *, limit: int = 10", ""), version=2)

        self.assertEqual(code, 0)
        self.assertIn("lookup(limit)", said)

    def test_an_addition_passes(self):
        added = SHAPES.replace("limit: int = 10", "limit: int = 10, page: int = 1") + "\n\ndef more() -> None: ...\n"
        code, said = self.judge(
            shapes=added, sdk=SDK.replace('"lookup"]', '"lookup", "more"]').replace("lookup\n", "lookup, more\n")
        )

        self.assertEqual(code, 0, said)

    def test_a_display_constructor_is_compared(self):
        """A class made by a dataclass_transform decorator gets its constructor compared, so
        reordering its fields breaks a positional call."""

        reordered = SHAPES.replace(
            '    title: str = ""\n    detail: str = ""\n', '    detail: str = ""\n    title: str = ""\n'
        )
        code, said = self.judge(shapes=reordered)

        self.assertEqual(code, 1)
        self.assertIn("Card.__init__(title)", said)
