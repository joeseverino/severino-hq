"""The tree git would write from a checkout's files as they stand, staged or
not, tracked or not: what a commit of exactly those files would hold.

It is built in a copy of the index, so the checkout's own is untouched, and a
copy already holds each unchanged file's hash, so git reads only what
changed. Stdlib only: the gate verdict imports it on a runner's bare python3.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path


def working_tree(root: Path, *, user_ignores: bool = True, leaving: Sequence[str] = ()) -> str:
    """The tree of the checkout at ``root``, as a commit of its files would
    hold it: the repository's ignore rules apply, and the user's too unless
    ``user_ignores`` is off. Each path in ``leaving`` is left out."""

    def git(*args: str, env: dict[str, str] | None = None) -> str:
        done = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True, env=env)
        return done.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="tree-") as scratch:
        index = Path(scratch) / "index"
        # A checkout with nothing yet staged has no index, and an empty one is the same start.
        real = Path(root, git("rev-parse", "--git-path", "index"))
        if real.is_file():
            shutil.copyfile(real, index)
        env = os.environ | {"GIT_INDEX_FILE": str(index)}
        excludes = () if user_ignores else ("-c", "core.excludesFile=/dev/null")
        git(*excludes, "add", "--all", env=env)
        if leaving:
            git("rm", "--cached", "--quiet", "--ignore-unmatch", "--", *leaving, env=env)
        return git("write-tree", env=env)
