"""git in a fixture repository, for the tests that build one."""

import subprocess
from collections.abc import Callable
from pathlib import Path

IDENTITY = ["-c", "user.name=Example", "-c", "user.email=dev@example.com", "-c", "commit.gpgsign=false"]


def fixture_git(root: Path) -> Callable[..., str]:
    """git in the fixture repository at ``root``, committing as an example
    identity, past any hook the machine's git config names, which judges real
    commits."""

    def git(*args: str) -> str:
        if args[:1] == ("commit",):
            args = ("commit", "--no-verify", *args[1:])
        done = subprocess.run(["git", *IDENTITY, *args], cwd=root, check=True, capture_output=True, text=True)
        return done.stdout.strip()

    return git
