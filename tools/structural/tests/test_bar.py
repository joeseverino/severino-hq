"""The structural bar runs the graph release mise.toml pins."""

import tomllib

from django.test import SimpleTestCase, tag

from tools.structural import bar


@tag("source")
class GraphToolTests(SimpleTestCase):
    def test_the_bar_installs_the_tool_mise_pins_and_runs_its_binary(self):
        """A lazy tool installs only when asked by name, and resolves by its binary."""
        declared = tomllib.loads((bar.ROOT / "mise.toml").read_text(encoding="utf-8"))["tools"]
        self.assertIn(bar.TOOL, declared[bar.PACKAGE].get("lazy_bins", []))
