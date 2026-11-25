"""The branch a host tree lands on, the one answer fast's base and the
composed extension set both read. Stdlib only: a pick of the extension set,
which every composing step asks first, loads none of HQ."""

from __future__ import annotations

import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Landing:
    """A branch a tree lands on: its name, as a pull request names it, and the ref it is read at."""

    branch: str
    ref: str


def git(root: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    return done.stdout.strip() if done.returncode == 0 else None


def landing(root: Path, among: Collection[str] | None = None) -> Landing | None:
    """The branch the tree at ``root`` lands on, of the local branches ``among`` (by default every one that
    tracks a remote one), or None.

    A branch with an upstream lands on it: a pushed one is a pull request's head, so it lands as
    itself, as CI reads it (tools/pipeline/coordinated-branches.sh). Else its own branch where ``among`` holds it, else the
    one of ``among`` it is nearest (fewest commits of its own off it, then fewest of the branch's it
    lacks), as a worktree made from that branch is; a tie is no landing. A detached tree lands nowhere:
    it is a gate's copy of one commit (test --at, the second machine's)."""

    if not (own := git(root, "symbolic-ref", "-q", "--short", "HEAD")):
        return None
    if upstream := git(root, "for-each-ref", "--format=%(upstream)", f"refs/heads/{own}"):
        # refs/remotes/<remote>/<branch>, or refs/heads/<branch> for a branch made to track a local one.
        return Landing(upstream.split("/", 3 if upstream.startswith("refs/remotes/") else 2)[-1], upstream)
    if among is None:
        listed = git(root, "for-each-ref", "--format=%(refname:lstrip=2) %(upstream)", "refs/heads") or ""
        among = {name for name, _, upstream in (line.partition(" ") for line in listed.splitlines()) if upstream}
    if own in among:
        return Landing(own, f"refs/heads/{own}")
    apart: dict[str, tuple[int, ...]] = {}
    for name in sorted(among):
        counts = (git(root, "rev-list", "--left-right", "--count", f"HEAD...refs/heads/{name}") or "").split()
        if len(counts) == 2:
            apart[name] = tuple(map(int, counts))
    nearest = [name for name, distance in apart.items() if distance == min(apart.values())]
    return Landing(nearest[0], f"refs/heads/{nearest[0]}") if len(nearest) == 1 else None
