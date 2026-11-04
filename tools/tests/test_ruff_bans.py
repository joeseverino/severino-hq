"""tools/ keeps every ban the root's Ruff config declares but the one on importing tools.

tools/ruff.toml states its banned-api whole, since a nested table replaces the
root's, so a ban added to pyproject.toml alone would be lifted in tools/.
"""

import tomllib

from django.test import SimpleTestCase, tag

from tests.mise_tasks import ROOT


def root_bans() -> dict[str, object]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return config["tool"]["ruff"]["lint"]["flake8-tidy-imports"]["banned-api"]


def tools_bans() -> dict[str, object]:
    config = tomllib.loads((ROOT / "tools" / "ruff.toml").read_text(encoding="utf-8"))
    return config["lint"]["flake8-tidy-imports"]["banned-api"]


@tag("source")
class ToolsBanTests(SimpleTestCase):
    def test_tools_bans_are_the_roots_but_importing_tools(self) -> None:
        root = root_bans()
        self.assertIn("tools", root)
        self.assertEqual(tools_bans(), {name: ban for name, ban in root.items() if name != "tools"})
