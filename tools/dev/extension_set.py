"""The extension set: named in this checkout's mise.local.toml from the extension checkouts themselves,
and picked from there for each host tree a step composes.

Each package is read the way admission reads it (tools.pipeline.plugin_identity),
so the set named here is the one Compose builds. mise writes the file, which
keeps every other value already in it.

`pick` is the one place a step that composes HQ with its extensions learns
which extension trees to load, so no step composes a set its host refuses.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tools.pipeline.plugin_identity import PLUGINS, identity, manifest_file, plugin_api_version, required_api, spoken

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / "mise.local.toml"
# A checkout is one package, or holds its packages a level or two down
# (packages/<name>/).
LAYOUTS = ("pyproject.toml", "*/pyproject.toml", "*/*/pyproject.toml")


def packages(checkout: Path) -> list[Path]:
    """Every package root in ``checkout``: a pyproject.toml beside a src/ directory."""

    found = {
        project.parent
        for layout in LAYOUTS
        for project in checkout.glob(layout)
        if (project.parent / "src").is_dir()
        and not any(part.startswith(".") for part in project.relative_to(checkout).parts)
    }
    return sorted(found)


def extension_set(checkouts: list[Path]) -> dict[str, str]:
    """The two values mise.local.toml names the set by, each as the environment would hold it."""

    roots = [root for checkout in checkouts for root in packages(checkout.resolve())]
    if not roots:
        raise SystemExit(
            f"No extension package in {', '.join(map(str, checkouts))}: none holds a pyproject.toml beside src/."
        )
    sources = ":".join(str(root / "src") for root in roots)
    if "'" in sources:
        raise SystemExit(f"An extension path holds a quote, which the file cannot carry: {sources}")
    return {
        "HQ_LOCAL_PYTHONPATH": sources,
        "HQ_LOCAL_PLUGINS": ",".join(identity(root)["plugin-reference"] for root in roots),
    }


def write(local: Path, named: dict[str, str]) -> None:
    """Set each of ``named`` in ``local`` through mise, which keeps every other value in it.

    A value already in the environment wins, so a worktree, which copies the
    file, composes another set by naming it on the command line.
    """

    templated = (f"{key}={{{{ get_env(name='{key}', default='{value}') }}}}" for key, value in named.items())
    subprocess.run(["mise", "set", "--file", str(local), *templated], check=True)


# ------------------------------------------------------------------- picking

# What names the set a step is asked to compose: mise.local.toml, or the environment, which wins.
NAMED = ("HQ_LOCAL_PYTHONPATH", "HQ_LOCAL_PLUGINS")
# Where a commit of an extension repository a host tree needs is unpacked, one folder per commit,
# made once: a commit's files never change.
SNAPSHOTS = ROOT / "var" / "extensions"
ALONE = "No HQ_LOCAL_PLUGINS supplied: the host alone, as the image runs it (mise run setup:extensions names a set)."


class Unspoken(Exception):
    """A host commit that names no plugin API, so no extension can be composed with it."""


@dataclass(frozen=True)
class Pick:
    """The extension trees a step composes for one host tree, and the line that says which and why."""

    sources: tuple[str, ...]
    plugins: tuple[str, ...]
    said: str
    # Each repository composed at a commit of its history rather than as it stands: its root and that commit.
    at: tuple[tuple[str, str], ...] = ()
    # Each repository composed from another worktree of it, as that stands: its root and that worktree.
    stood: tuple[tuple[str, str], ...] = ()

    @property
    def named(self) -> dict[str, str]:
        """The set as the environment names it, for a step that picks again (a base's server)."""

        return {"HQ_LOCAL_PYTHONPATH": ":".join(self.sources), "HQ_LOCAL_PLUGINS": ",".join(self.plugins)}

    @property
    def composed(self) -> dict[str, str]:
        """What HQ loads the set from; admission off, since a checkout has no admitted wheels."""

        return {
            "SEVERINO_HQ_PLUGINS": ",".join(self.plugins),
            "PYTHONPATH": ":".join(self.sources),
            "SEVERINO_HQ_REQUIRE_PLUGIN_ADMISSION": "0",
        }


def _git(top: Path, *args: str) -> str | None:
    done = subprocess.run(["git", "-C", str(top), *args], capture_output=True, text=True, check=False)
    return done.stdout if done.returncode == 0 else None


def _required_at(top: Path, commit: str, manifest: str) -> int | None:
    """The API the manifest at ``manifest`` in ``top`` requires at ``commit``, or None where it holds none."""

    if (source := _git(top, "show", f"{commit}:{manifest}")) is None:
        return None
    try:
        return required_api(source)
    except SystemExit:
        return None


def moved_past(repository: Path, commit: str) -> int | None:
    """When the host first moved past ``commit`` of ``repository``, or None while nothing has.

    An extension's commit is made against the host it lands beside, and may use what that host
    added at the same plugin API, so the commits that fit ``commit`` are those made before the
    host moved on from it: the earliest commit made on it. A commit named is read on HEAD's own
    line where it is on it, which every clone of the tree holds alike (the gate's second machine
    included). A tree's ``HEAD`` is read from the shared history it stands on, the newest of its
    commits on a branch or remote but its own branch, past which the rest are its own work."""

    sha = (_git(repository, "rev-parse", "--verify", f"{commit}^{{commit}}") or "").strip()
    if not sha:
        return None
    unshared: list[str] = []
    line = "HEAD" if _git(repository, "merge-base", "--is-ancestor", sha, "HEAD") is not None else "--all"
    if commit == "HEAD":
        line = "--all"
        own = (_git(repository, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()
        others = [*([f"--exclude={own}"] if own else []), "--branches", "--remotes"]
        unshared = (_git(repository, "rev-list", "--topo-order", sha, "--not", *others) or "").split()
        if unshared:
            parents = (_git(repository, "rev-list", "--parents", "-n", "1", unshared[-1]) or "").split()[1:2]
            if not parents:
                return None
            sha = parents[0]
    children = _git(repository, "log", "--ancestry-path", "--format=%H %P %ct", line, f"^{sha}") or ""
    times = [
        int(fields[-1])
        for fields in map(str.split, children.splitlines())
        if sha in fields[1:-1] and fields[0] not in unshared
    ]
    return min(times, default=None)


def matching_commit(top: Path, manifests: list[str], api: int, before: int | None = None) -> str | None:
    """The newest commit on ``top``'s HEAD, made before ``before`` when given, at which every manifest
    requires ``api``, or None.

    A run of commits at one API ends where a commit changes a manifest's api_version, so the
    commits worth asking are the newest and, for each such change, the commit before it and the change."""

    bounded = () if before is None else (f"--before={before - 1}",)
    if not (newest := (_git(top, "rev-list", "-1", *bounded, "HEAD") or "").strip()):
        return None
    changes = _git(top, "log", "--format=%H", "-G", "api_version", newest, "--", *manifests)
    if changes is None:
        return None
    asked = [newest, *(each for change in changes.split() for each in (f"{change}^", change))]
    for commit in dict.fromkeys(asked):
        if all(_required_at(top, commit, manifest) == api for manifest in manifests):
            return (_git(top, "rev-parse", "--verify", f"{commit}^{{commit}}") or "").strip() or None
    return None


def snapshot(top: Path, commit: str, packages: list[str]) -> Path:
    """``packages`` of ``top`` as they stand at ``commit``, unpacked under SNAPSHOTS once."""

    folder = SNAPSHOTS / f"{top.name}-{commit[:12]}"
    if folder.is_dir():
        return folder
    archive = subprocess.run(
        ["git", "-C", str(top), "archive", "--format=tar", commit, "--", *packages], capture_output=True, check=True
    ).stdout
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    # Unpacked beside its name and moved there whole, so a step started at the same time never reads half
    # of it; the one that moves second finds the folder made.
    unpacking = tempfile.mkdtemp(dir=SNAPSHOTS, prefix=".unpacking-")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(unpacking, filter="data")
        try:
            Path(unpacking).replace(folder)
        except OSError:
            if not folder.is_dir():
                raise
    finally:
        shutil.rmtree(unpacking, ignore_errors=True)
    return folder


def branches(top: Path) -> dict[str, Path | None]:
    """Every local branch of the repository ``top`` is in, with the worktree checked out on it, if any."""

    trees: dict[str, Path] = {}
    tree = None
    for line in (_git(top, "worktree", "list", "--porcelain") or "").splitlines():
        if line.startswith("worktree "):
            tree = Path(line.removeprefix("worktree "))
        elif line.startswith("branch refs/heads/") and tree is not None and tree.is_dir():
            trees[line.removeprefix("branch refs/heads/")] = tree
    names = (_git(top, "for-each-ref", "--format=%(refname)", "refs/heads") or "").split()
    return {name.removeprefix("refs/heads/"): trees.get(name.removeprefix("refs/heads/")) for name in names}


def default_branch(top: Path) -> str:
    """The branch ``top``'s repository merges into: its remote's HEAD, else the one git names a new repository's."""

    if head := (_git(top, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "").strip():
        return head.split("/", 1)[-1]
    return (_git(top, "config", "--get", "init.defaultBranch") or "").strip() or "master"


def landing(host: Path, carried: set[str], default: str) -> str | None:
    """The branch of ``carried`` the host tree at ``host`` lands on, or None where it is ``default`` or unsure.

    Its own branch where ``carried`` holds it, else the host branch it is nearest (fewest commits of its own
    off it, then fewest of the branch's it lacks), as a worktree made from that branch is. A tie with
    ``default``, or between two, is no landing: CI composes a branch only for a pull request into the default.
    A detached tree lands nowhere: it is a gate's copy of one commit (test --at, the second machine's), whose
    set was picked where it was sent from."""

    if not (own := (_git(host, "symbolic-ref", "-q", "--short", "HEAD") or "").strip()):
        return None
    if own in carried:
        return None if own == default else own
    hosts = {
        name.removeprefix("refs/heads/")
        for name in (_git(host, "for-each-ref", "--format=%(refname)", "refs/heads") or "").split()
    }
    apart: dict[str, tuple[int, ...]] = {}
    for name in sorted((carried | {default}) & hosts):
        counts = (_git(host, "rev-list", "--left-right", "--count", f"HEAD...refs/heads/{name}") or "").split()
        if len(counts) == 2:
            apart[name] = tuple(map(int, counts))
    if not apart:
        return None
    nearest = [name for name, distance in apart.items() if distance == min(apart.values())]
    return nearest[0] if len(nearest) == 1 and nearest[0] != default else None


@dataclass(frozen=True)
class _Package:
    source: str
    root: Path
    top: Path
    required: int


def _package(source: str) -> _Package | None:
    """The extension package ``source`` (a ``src`` directory) imports from, or None for a source that is no
    package's (a host fixture, a flat module), which loads with whatever host it is beside."""

    root = Path(source).resolve().parent
    if not (root / "pyproject.toml").is_file() or not manifest_file(root).is_file():
        return None
    top = _git(root, "rev-parse", "--show-toplevel")
    return _Package(source, root, Path(top.strip()) if top else root, required_api(manifest_file(root).read_text()))


def _required_in(root: Path) -> int | None:
    try:
        return required_api(manifest_file(root).read_text())
    except OSError, SystemExit:
        return None


@dataclass(frozen=True)
class _Coordinated:
    """A repository composed from its branch named as the host's: where each source moved, and why."""

    moved: dict[str, str]
    said: str
    at: tuple[str, str] | None = None
    stood: tuple[str, str] | None = None


def _coordinated(host: Path, top: Path, mine: list[_Package], api: int) -> _Coordinated | None:
    """``mine``, the packages of ``top``'s repository, from its branch named as the one the host tree at
    ``host`` lands on, as CI's composition builds an extension's branch of the host branch's name
    (tools/pipeline/coordinated-branches.sh); None where the repository carries no such branch."""

    known = branches(top)
    if (branch := landing(host, set(known), default_branch(top))) is None:
        return None
    relative = [package.root.relative_to(top).as_posix() for package in mine]
    common = (_git(top, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip()
    named = f"branch {branch} of {Path(common).parent.name if common else top.name}"
    if (tree := known[branch]) is None:
        commit = (_git(top, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}") or "").strip()
        base = snapshot(top, commit, relative)
        whence = f"{named} at {commit[:12]}"
    else:
        base = tree
        whence = f"{named} as it stands in {tree}"
    required = {_required_in(base / path) for path in relative}
    other = sorted(str(each) for each in required if each != api)
    refused = f" (it requires API {', '.join(other)}, which this host refuses)" if other else ""
    return _Coordinated(
        {
            package.source: str(base / path / Path(package.source).name)
            for package, path in zip(mine, relative, strict=True)
        },
        f"{whence}, the branch named as the one HQ lands on, as CI composes it{refused}",
        (str(top), commit) if tree is None else None,
        (str(top), str(tree)) if tree is not None and tree != top else None,
    )


def pick(
    speaks: Callable[[], int],
    where: str,
    *,
    before: int | None = None,
    as_they_stand: bool = True,
    host: Path | None = None,
    environ: Mapping[str, str] = os.environ,
) -> Pick:
    """The set ``environ`` names, as the host at ``where``, which ``speaks`` the plugin API it returns,
    can load it.

    Where ``host`` is the host's tree and a package's repository carries a branch named as the one that
    tree lands on, that branch is composed, as CI composes a coordinated change: the worktree checked out
    on it as it stands, else the branch's commit, unpacked under SNAPSHOTS. Each other package is asked
    the API it requires. Where every one requires ``api`` and ``as_they_stand``, the trees named are
    composed as they stand, uncommitted work included. Otherwise each repository's newest commit whose
    manifests all require ``api``, made before ``before`` when given, is composed instead, unpacked under
    SNAPSHOTS. Where it has none, no extension is composed, since the host refuses a package of another
    API."""

    plugins = tuple(each for each in environ.get("HQ_LOCAL_PLUGINS", "").split(",") if each)
    sources = tuple(each for each in environ.get("HQ_LOCAL_PYTHONPATH", "").split(":") if each)
    if not plugins:
        return Pick((), (), ALONE)
    api = speaks()
    packages = {source: found for source in sources if (found := _package(source))}
    if not sources:
        return Pick((), plugins, f"Extensions composed: {', '.join(plugins)}, from the host's own tree")
    moved: dict[str, str] = {}
    froms = []
    at = []
    stood = []
    apart: dict[Path, list[_Package]] = {}
    for top in dict.fromkeys(package.top for package in packages.values()):
        mine = [package for package in packages.values() if package.top == top]
        if host is not None and (coordinated := _coordinated(host, top, mine, api)):
            moved |= coordinated.moved
            froms.append(coordinated.said)
            at.extend([coordinated.at] if coordinated.at else [])
            stood.extend([coordinated.stood] if coordinated.stood else [])
        elif behind := [package for package in mine if package.required != api or not as_they_stand]:
            apart[top] = behind
    if not apart and not froms:
        return Pick(
            sources,
            plugins,
            f"Extensions composed from: {', '.join(sources)} (HQ_LOCAL_PYTHONPATH; one on the command line"
            " composes another tree)",
        )
    for top, behind in apart.items():
        mine = [package for package in packages.values() if package.top == top]
        relative = [package.root.relative_to(top).as_posix() for package in mine]
        manifests = [manifest_file(package.root).relative_to(top).as_posix() for package in mine]
        required = sorted({str(package.required) for package in behind if package.required != api})
        stands = f" (as it stands it requires API {', '.join(required)})" if required else ""
        made = " made before HQ moved on from it" if before is not None else ""
        commit = matching_commit(top, manifests, api, before)
        if commit is None:
            return Pick(
                (),
                (),
                f"No extension composed: HQ at {where} supports plugin API {api}, and no commit of {top}{made}"
                f" declares it{stands}; HQ_LOCAL_PYTHONPATH naming a tree at API {api} composes one.",
            )
        unpacked = snapshot(top, commit, relative)
        for package, path in zip(mine, relative, strict=True):
            moved[package.source] = str(unpacked / path / Path(package.source).name)
        froms.append(f"{top.name} at {commit[:12]}, its newest commit at API {api}{made}{stands}")
        at.append((str(top), commit))
    said = f"Extensions composed for HQ at {where}, which supports plugin API {api}, from {'; '.join(froms)}"
    chosen = tuple(moved.get(source, source) for source in sources)
    return Pick(
        chosen,
        plugins,
        f"{said}: {', '.join(chosen)}",
        tuple(at),
        tuple(stood),
    )


def for_tree(tree: Path, environ: Mapping[str, str] = os.environ) -> Pick:
    """The set a step composes for the host checked out at ``tree``: an extension's branch named as the
    one the tree lands on where its repository carries one, as CI composes it."""

    return pick(
        lambda: int(plugin_api_version(tree)), str(tree), before=moved_past(tree, "HEAD"), host=tree, environ=environ
    )


def at_commit(commit: str, environ: Mapping[str, str] = os.environ) -> Pick:
    """The set a step composes for the host at ``commit`` of this checkout, read without checking it out:
    the extensions as they were when the host moved past it, never as they stand, which may use what
    the host added since at the same plugin API."""

    def speaks() -> int:
        if (source := _git(ROOT, "show", f"{commit}:{PLUGINS.as_posix()}")) is None:
            raise Unspoken(f"No extension composed: {commit[:12]} has no {PLUGINS}, so it names no plugin API.")
        return int(spoken(f"{commit[:12]}:{PLUGINS}", source))

    try:
        return pick(speaks, commit[:12], before=moved_past(ROOT, commit), as_they_stand=False, environ=environ)
    except Unspoken as said:
        return Pick((), (), str(said))


def shell() -> None:
    """For tools/lib/extension-set.sh: the pick for the tree in argv[1] as three lines, its sources, its
    plugins and what it says."""

    picked = for_tree(Path(sys.argv[1]).resolve())
    print(":".join(picked.sources), ",".join(picked.plugins), picked.said, sep="\n")


def main() -> int:
    # Imported here, so a pick, which every composing step asks first, loads none of HQ.
    from hq.platform.core.tests.task import usage

    checkouts = [Path(each).expanduser() for each in usage().repeated("checkout")]
    named = extension_set(checkouts)
    write(LOCAL, named)
    print(f"{LOCAL.name} names {named['HQ_LOCAL_PLUGINS']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
