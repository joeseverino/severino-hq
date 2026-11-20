"""Renovate runs what its configuration names and finds every pin it is told to watch."""

import json
import re
import subprocess
from pathlib import PurePosixPath

from django.test import SimpleTestCase, tag

from hq.platform.application.tests.reads import reads
from hq.platform.application.tests.test_pipeline import ROOT, WORKFLOWS, every_step


@tag("source", "tree")
# Each file a custom manager's pattern matches, which a pattern can match anywhere an add puts one.
@reads(
    ".github/renovate.json",
    ".github/workflows/renovate.yml",
    "deploy/renovate/config.json",
    "hk.pkl",
    "mise.toml",
    "static/vendor/scalar/UPSTREAM",
    "tools/pipeline/toolchain.env",
    "deploy/renovate/image",
    lists=("**",),
)
class RenovateTests(SimpleTestCase):
    def test_every_command_the_configuration_runs_is_one_the_workflow_allows(self):
        """Renovate skips a post-upgrade command its runner does not allow, and the
        pull request then carries a version bump without what it needed."""
        config = json.loads((ROOT / ".github" / "renovate.json").read_text())
        commands = [
            command
            for rule in config["packageRules"]
            for command in rule.get("postUpgradeTasks", {}).get("commands", [])
        ]
        (global_configuration,) = [
            step["with"]["configurationFile"]
            for step in every_step(WORKFLOWS / "renovate.yml")
            if step.get("uses", "").startswith("renovatebot/github-action@")
        ]
        allowed = json.loads((ROOT / global_configuration).read_text())["allowedCommands"]
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertTrue(any(re.search(pattern, command) for pattern in allowed))

    def test_every_custom_manager_finds_its_pin(self):
        """A file that moves without its pattern, or a line reworded under its
        match string, leaves the pin unwatched and Renovate says nothing."""
        tracked = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split("\0")
        managers = json.loads((ROOT / ".github" / "renovate.json").read_text())["customManagers"]
        self.assertTrue(managers)
        for manager in managers:
            with self.subTest(manager=manager["description"]):
                patterns = manager.get("managerFilePatterns", manager.get("fileMatch", []))
                self.assertTrue(patterns)
                named: set[str] = set()
                for pattern in patterns:
                    files = {name for name in tracked if name and renovate_file_pattern_matches(pattern, name)}
                    self.assertTrue(files, f"{pattern} matches no tracked file")
                    named |= files
                for match_string in manager["matchStrings"]:
                    expression = re.compile(match_string.replace("(?<", "(?P<"))
                    self.assertTrue(
                        any(expression.search((ROOT / name).read_text()) for name in named),
                        f"{match_string} matches nothing in {sorted(named)}",
                    )


def renovate_file_pattern_matches(pattern: str, name: str) -> bool:
    """Renovate reads ``/…/`` as a regular expression and anything else as a glob."""
    if len(pattern) > 1 and pattern.startswith("/") and pattern.endswith("/"):
        return re.search(pattern[1:-1], name) is not None
    return PurePosixPath(name).full_match(pattern)
