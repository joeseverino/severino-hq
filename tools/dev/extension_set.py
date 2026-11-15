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


def matching_commit(top: Path, manifests: list[str], api: int) -> str | None:
    """The newest commit on ``top``'s HEAD at which every manifest requires ``api``, or None.

    A run of commits at one API ends where a commit changes a manifest's api_version, so the
    commits worth asking are HEAD and, for each such change, the commit before it and the change."""

    changes = _git(top, "log", "--format=%H", "-G", "api_version", "HEAD", "--", *manifests)
    if changes is None:
        return None
    asked = ["HEAD", *(each for change in changes.split() for each in (f"{change}^", change))]
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


def pick(speaks: Callable[[], int], where: str, environ: Mapping[str, str] = os.environ) -> Pick:
    """The set ``environ`` names, as the host at ``where``, which ``speaks`` the plugin API it returns,
    can load it.

    Each package is asked the API it requires. Where every one requires ``api``, the trees named are
    composed as they stand, uncommitted work included. Where a repository's do not, its newest commit
    whose manifests all require ``api`` is composed instead, unpacked under SNAPSHOTS. Where it has
    none, no extension is composed, since the host refuses a package of another API."""

    plugins = tuple(each for each in environ.get("HQ_LOCAL_PLUGINS", "").split(",") if each)
    sources = tuple(each for each in environ.get("HQ_LOCAL_PYTHONPATH", "").split(":") if each)
    if not plugins:
        return Pick((), (), ALONE)
    api = speaks()
    packages = {source: found for source in sources if (found := _package(source))}
    apart = {}
    for package in packages.values():
        if package.required != api:
            apart.setdefault(package.top, []).append(package)
    if not sources:
        return Pick((), plugins, f"Extensions composed: {', '.join(plugins)}, from the host's own tree")
    if not apart:
        return Pick(
            sources,
            plugins,
            f"Extensions composed from: {', '.join(sources)} (HQ_LOCAL_PYTHONPATH; one on the command line"
            " composes another tree)",
        )
    moved: dict[str, str] = {}
    froms = []
    for top, behind in apart.items():
        mine = [package for package in packages.values() if package.top == top]
        relative = [package.root.relative_to(top).as_posix() for package in mine]
        manifests = [manifest_file(package.root).relative_to(top).as_posix() for package in mine]
        requires = f"as it stands it requires API {', '.join(sorted({str(package.required) for package in behind}))}"
        commit = matching_commit(top, manifests, api)
        if commit is None:
            return Pick(
                (),
                (),
                f"No extension composed: HQ at {where} supports plugin API {api}, {top} {requires}, and no commit"
                f" on its HEAD declares {api}; HQ_LOCAL_PYTHONPATH naming a tree at API {api} composes one.",
            )
        unpacked = snapshot(top, commit, relative)
        for package, path in zip(mine, relative, strict=True):
            moved[package.source] = str(unpacked / path / Path(package.source).name)
        froms.append(f"{top.name} at {commit[:12]}, its newest commit at API {api} ({requires})")
    chosen = tuple(moved.get(source, source) for source in sources)
    return Pick(
        chosen,
        plugins,
        f"Extensions composed from {'; '.join(froms)}, the API HQ at {where} supports: {', '.join(chosen)}",
    )


def for_tree(tree: Path, environ: Mapping[str, str] = os.environ) -> Pick:
    """The set a step composes for the host checked out at ``tree``."""

    return pick(lambda: int(plugin_api_version(tree)), str(tree), environ)


def at_commit(commit: str, environ: Mapping[str, str] = os.environ) -> Pick:
    """The set a step composes for the host at ``commit`` of this checkout, read without checking it out."""

    def speaks() -> int:
        if (source := _git(ROOT, "show", f"{commit}:{PLUGINS.as_posix()}")) is None:
            raise Unspoken(f"No extension composed: {commit[:12]} has no {PLUGINS}, so it names no plugin API.")
        return int(spoken(f"{commit[:12]}:{PLUGINS}", source))

    try:
        return pick(speaks, commit[:12], environ)
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
