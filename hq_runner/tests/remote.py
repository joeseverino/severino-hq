"""A git repository on this machine standing in for the one a worker's
procedures come from, reached as ``<origin>/<repository>.git`` the way the
worker reaches GitHub, over git's file transport instead of https."""

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

REPOSITORY = "example/automation"
# Git as a fixture runs it: none of this machine's own configuration or hooks.
_ENVIRONMENT = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Example",
    "GIT_AUTHOR_EMAIL": "automation@example.com",
    "GIT_COMMITTER_NAME": "Example",
    "GIT_COMMITTER_EMAIL": "automation@example.com",
    # One moment for every commit, so a commit of the same files after the
    # same parent is the same commit, which a recorded scan names.
    "GIT_AUTHOR_DATE": "2026-10-03T12:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-10-03T12:00:00+00:00",
}


class Remote:
    """A repository under ``root`` and the origin a worker's project names it by."""

    def __init__(self, root: Path) -> None:
        self.origin = root.as_uri()
        self.path = root / f"{REPOSITORY}.git"
        self.path.mkdir(parents=True)
        self.git("init", "--quiet", "--initial-branch", "main")

    def git(self, *arguments: str) -> str:
        done = subprocess.run(
            ["git", "-C", str(self.path), *arguments],
            env=_ENVIRONMENT,
            capture_output=True,
            text=True,
            check=True,
        )
        return done.stdout.strip()

    def commit(self, files: Mapping[str, str], *, links: Mapping[str, str] | None = None) -> str:
        """Commit ``files`` (path to text) and ``links`` (path to link target); answer the commit's id."""

        for name, text in files.items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        for name, target in (links or {}).items():
            path = self.path / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.unlink(missing_ok=True)
            path.symlink_to(target)
        self.git("add", "--all")
        self.git("-c", "commit.gpgsign=false", "commit", "--no-verify", "--quiet", "--allow-empty", "-m", "procedures")
        return self.git("rev-parse", "HEAD")

    def tag(self, name: str) -> str:
        """An annotated tag of the current commit; answer the tag object's own id."""

        self.git("-c", "tag.gpgsign=false", "tag", "--annotate", "--message", name, name)
        return self.git("rev-parse", name)
