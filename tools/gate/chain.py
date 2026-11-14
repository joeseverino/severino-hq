"""fast's chain of passed trees: the base a run reads its change from, and
the record a pass leaves for the next run (docs/Development.md, What `fast`
runs)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from hq.platform.core.tests.task import RefError, git
from tools.gate import testmap

ROOT = Path(__file__).resolve().parents[2]


# How far back first-parent history is read for a commit whose tree passed.
PASSED_REACH = 300


def fast_base(asked: str | None) -> tuple[str, str]:
    """(the commit the change is read from, why it is the one).

    With none asked for, the nearest commit whose tree passed: what the change
    from it leaves unselected had passed, which is what lets ``record_pass``
    record this tree. Without one in reach, the merge base with the branch
    HEAD lands on."""

    if asked:
        return merge_base(asked), f"the merge base with {asked}"
    if found := passed_ancestor(ROOT):
        commit, back = found
        return commit, f"the nearest commit whose tree passed, {back} back on first-parent history"
    reason = f"no commit within {PASSED_REACH} back has a passed tree, so the merge base with the branch HEAD lands on"
    return merge_base(None), reason


def passed_ancestor(root: Path) -> tuple[str, int] | None:
    """(the nearest commit on HEAD's first-parent history whose tree passed,
    how many commits back), or None within PASSED_REACH."""

    return recorded_ancestor(passes(root), "", root)


def recorded_ancestor(folder: Path, suffix: str, root: Path) -> tuple[str, int] | None:
    """(the nearest commit on HEAD's first-parent history whose tree has a
    file named by its hash and ``suffix`` in ``folder``, how many commits
    back), or None within PASSED_REACH."""

    if not folder.is_dir():
        return None
    history = answered("log", "--first-parent", f"--max-count={PASSED_REACH}", "--format=%H %T", "HEAD", root=root)
    for back, line in enumerate(history):
        commit, _, tree = line.partition(" ")
        if (folder / f"{tree}{suffix}").is_file():
            return commit, back
    return None


def merge_base(base: str | None) -> str:
    """The merge base with the named base, or else with the branch HEAD lands
    on, or else with the first default that exists.

    A base that was asked for and does not resolve fails: falling back would
    quietly test a different change.
    """

    for candidate in (base,) if base else (*landing(), "origin/main", "main"):
        try:
            return git("merge-base", "HEAD", candidate)[0]
        except RefError, IndexError:
            if base:
                sys.exit(f"fast: {base} has no merge base with HEAD; pass a ref that exists")
    sys.exit("fast: no base found; pass one (mise run fast <ref>)")


def landing(root: Path = ROOT) -> list[str]:
    """The branch HEAD lands on: its own branch's upstream, or else the local
    branch tracking a remote one whose history HEAD left most recently. A
    worktree's branch tracks nothing and is made from the branch it lands on,
    which may be far ahead of main, so main would select most of the suite."""

    if upstream := answered("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", root=root):
        return upstream
    current = git("branch", "--show-current", root=root)
    own = f"refs/heads/{current[0]}" if current else ""
    listed = git("for-each-ref", "--format=%(refname) %(upstream)", "refs/heads", root=root)
    tracking = [ref for ref, _, upstream in (line.partition(" ") for line in listed) if upstream and ref != own]
    left = {
        ref: int(git("rev-list", "--count", f"{fork[0]}..HEAD", root=root)[0])
        for ref in tracking
        if (fork := answered("merge-base", "HEAD", ref, root=root))
    }
    return sorted(left, key=left.__getitem__)[:1]


def answered(*args: str, root: Path = ROOT) -> list[str]:
    """What a git command prints, or nothing where git refuses (no upstream, no history in common)."""

    try:
        return git(*args, root=root)
    except RefError:
        return []


# Where a passed tree is recorded, under the repository's common git directory
# so every worktree shares it: an empty file named by the tree's hash, which a
# commit keeps when it is re-signed. The agent hooks read it to hold a brief's
# base to a pass.
PASSES = ("severino", "fast-passes")


def passes(root: Path = ROOT) -> Path:
    common = git("rev-parse", "--path-format=absolute", "--git-common-dir", root=root)[0]
    return Path(common).joinpath(*PASSES)


def maps(root: Path = ROOT) -> Path:
    """Where the maps of the tests that ran each file are kept, beside the passed trees."""

    return passes(root).parent / testmap.MAPS


def tree_of(commit: str) -> str:
    return git("rev-parse", f"{commit}^{{tree}}")[0]


def record_pass(tree: str | None, base: str, *, whole: bool) -> None:
    """Record ``tree``, the one a passing run judged (None when the checkout
    changed in the run), and say so, when the run proves it: it selected the
    whole suite or the base's tree passed too, here or in CI on GitHub
    (``passed_in_ci``), so what the change left unselected is what had
    passed. A run that selected only what changed from an unpassed base
    proves nothing about the rest."""

    if not tree:
        return
    folder = passes()
    base_tree = tree_of(base)
    unproven = None if whole or (folder / base_tree).is_file() else passed_in_ci(base)
    if unproven:
        said = f"not recorded as passed: its base's tree has no pass here, and {unproven}"
    else:
        folder.mkdir(parents=True, exist_ok=True)
        if not whole and not (folder / base_tree).is_file():
            # Recorded first, so the next run reads its base's pass from here.
            (folder / base_tree).touch()
            print(f"[fast] base {base[:9]} passed CI on GitHub, so its tree {base_tree[:9]} is recorded as passed")
        (folder / tree).touch()
        said = "recorded as passed"
    print(f"[fast] tree {tree[:9]} {said}", flush=True)


# The workflow whose run on a pushed commit is every gate CI holds it to: on a
# pull request its Ready job is the check the ruleset requires, and on a push
# to main it runs each gate or finds them proven on the pull request that
# merged the same tree. Only a run of this repository's own commit counts: a
# fork's pull request runs a workflow its author wrote.
WORKFLOW = "ci.yml"
# How long GitHub has to answer before the run goes on unrecorded.
GITHUB_WAIT = 30


def passed_in_ci(commit: str) -> str | None:
    """None when CI's last completed run on ``commit`` passed; otherwise what
    kept it from counting, in words. It asks GitHub once, through gh, and an
    error, no network or a commit GitHub lacks is an answer, never a failure."""

    # GitHub finds a run by the whole hash alone.
    commit = next(iter(answered("rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")), commit)
    endpoint = f"repos/{{owner}}/{{repo}}/actions/workflows/{WORKFLOW}/runs"
    ask = ["gh", "api", "--method", "GET", endpoint, "-f", f"head_sha={commit}", "-f", "status=completed"]
    try:
        done = subprocess.run(ask, cwd=ROOT, capture_output=True, text=True, timeout=GITHUB_WAIT, check=False)
    except OSError, subprocess.TimeoutExpired:
        return f"GitHub did not answer whether CI passed {commit[:9]}"
    if done.returncode:
        reason = (done.stderr.strip().splitlines() or ["gh failed"])[-1]
        return f"GitHub did not say whether CI passed {commit[:9]} ({reason})"
    # Newest first, as GitHub lists them.
    own = [
        run
        for run in json.loads(done.stdout).get("workflow_runs", [])
        if (run.get("head_repository") or {}).get("full_name") == (run.get("repository") or {}).get("full_name")
    ]
    if not own:
        return f"CI has no completed run on {commit[:9]}"
    if own[0].get("conclusion") != "success":
        return f"CI's last run on {commit[:9]} ended {own[0].get('conclusion')}"
    return None
