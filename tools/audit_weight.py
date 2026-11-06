"""audit:dependency-weight's Python half: each locked distribution's installed size, largest first.

uv.lock and ``uv tree --show-sizes`` hold only compressed wheel sizes, so a distribution weighs
the sizes its RECORD lists, the installer's account of every file it wrote, as
``importlib.metadata`` reads them from this environment. ``uv export`` names the locked set, and
without the default groups the runtime set; every other locked distribution is a dev group's.
"""

import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Iterable, Mapping
from importlib.metadata import distributions

from packaging.utils import canonicalize_name

KIB = 1024
MIB = 1024 * 1024
RUNTIME, DEV = "runtime", "dev"
ABSENT = ", not installed here"


def locked(*groups: str) -> dict[str, str]:
    """Each distribution the lock names for ``groups``, by its normalized name, with its version."""

    exported = subprocess.run(
        ["uv", "export", "--frozen", "--quiet", "--format", "pylock.toml", "--no-emit-project", *groups],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {canonicalize_name(entry["name"]): entry["version"] for entry in tomllib.loads(exported)["packages"]}


def installed(path: list[str] | None = None) -> dict[str, int]:
    """Each distribution on ``path`` (this environment's without one), by its normalized name, with
    the bytes its RECORD lists."""

    found = distributions(path=path) if path is not None else distributions()
    return {
        canonicalize_name(dist.metadata["Name"]): sum(file.size or 0 for file in dist.files or ()) for dist in found
    }


def report(every: Mapping[str, str], runtime: Iterable[str], sizes: Mapping[str, int]) -> list[str]:
    """One line per locked distribution, largest first, then the totals."""

    runtime = set(runtime)
    total: Counter[str] = Counter()
    count: Counter[str] = Counter()
    rows = []
    for name, version in every.items():
        kind = RUNTIME if name in runtime else DEV
        size = sizes.get(name, 0)
        total[kind] += size
        count[kind] += 1
        rows.append((size, f"{size / KIB:10.1f} KiB  {kind + ('' if name in sizes else ABSENT):<26} {name} {version}"))
    strays = [size for name, size in sizes.items() if name not in every]
    rows.sort(key=lambda row: (-row[0], row[1]))
    totals = (
        f"Python: {count[RUNTIME]} runtime distributions, {total[RUNTIME] / MIB:.1f} MiB installed; "
        f"{count[DEV]} are the dev groups, {total[DEV] / MIB:.1f} MiB; "
        f"{len(strays)} installed but not locked, {sum(strays) / MIB:.1f} MiB"
    )
    return [*(line for _, line in rows), totals]


def main() -> int:
    every = locked("--all-groups")
    runtime = locked("--no-default-groups")
    print(*report(every, runtime, installed()), sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
