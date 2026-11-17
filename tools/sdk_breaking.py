"""lint:sdk-breaking: a change that breaks what ``hq_sdk`` offers an extension fails, unless
``PLUGIN_API_VERSION`` moves with it.

Griffe reads ``hq_sdk`` here and at the base, statically, and judges each change by its own checks
(``find_breaking_changes``). Its ``check`` command is not used: it resolves no import from outside
the package, and the SDK's exports are re-exported from ``hq``, so it would compare names alone; and
it checks the base out on a branch named for the ref, which two worktrees checking against one base
cannot both create. An addition breaks nothing, so there is nothing to regenerate.

    python -m tools.sdk_breaking [base]

The base is fast's: the merge base with the ref named, or else with the branch HEAD lands on.
The breaks listed are those since that base. The version admits them when it differs from the
base's or from the one released extensions speak, read at the merge base with the default branch:
a branch stacked on another that already moved the version lands with it as one release, so each
later break on it is admitted by that one move.
"""

import io
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import griffe

from tools.gate.chain import ROOT, answered, merge_base, released_fork
from tools.pipeline.plugin_identity import PLUGINS, plugin_api_version, spoken

PACKAGE = "hq_sdk"
# The SDK and the host modules it re-exports from.
SOURCES = (":(glob)hq_sdk/**/*.py", ":(glob)hq/**/*.py")
# A breakage removed at the base reads its file from the base's copy; under a directory named so,
# griffe prints that file as the repository names it (its diff.py, Breakage._location).
BASE_PREFIX = "griffe-worktree-"
_COLOR = re.compile(r"\x1b\[[0-9;]*m")


class TransformedDataclasses(griffe.Extension):
    """A class made by a decorator that carries ``dataclass_transform`` (PEP 681), as
    ``hq.platform.application.display_words.display`` does, gets the ``__init__`` griffe gives a
    dataclass, so its constructor is compared too. It goes once griffe's dataclasses extension
    honors PEP 681, which it reads only ``dataclasses.dataclass`` for."""

    def on_package(self, *, pkg: griffe.Module, **kwargs: Any) -> None:
        self.walk(pkg)

    def walk(self, parent: griffe.Module | griffe.Class) -> None:
        for member in parent.members.values():
            if member.is_alias:
                continue
            if member.is_class:
                self.mark(member)
            if member.is_module or member.is_class:
                self.walk(member)

    def mark(self, cls: griffe.Class) -> None:
        for decorator in cls.decorators:
            value = decorator.value
            if not isinstance(value, griffe.Expr) or not transforms(cls, value.canonical_path):
                continue
            arguments = value.arguments if isinstance(value, griffe.ExprCall) else []
            # The built-in dataclasses extension, which runs after this one, builds the __init__.
            decorator.value = griffe.ExprCall(griffe.ExprName("dataclass", parent="dataclasses"), arguments)


def transforms(cls: griffe.Class, path: str) -> bool:
    """Whether the function at ``path`` is decorated with ``dataclass_transform``."""

    try:
        function = cls.modules_collection[path]
    except KeyError, griffe.AliasResolutionError:
        return False
    return function.is_function and any(
        isinstance(d.value, griffe.Expr) and d.value.canonical_path.endswith(".dataclass_transform")
        for d in function.decorators
    )


def load(root: Path) -> griffe.Module:
    return griffe.load(
        PACKAGE,
        search_paths=[root],
        try_relative_path=False,
        extensions=griffe.load_extensions(TransformedDataclasses),
        resolve_aliases=True,
        resolve_external=True,
        allow_inspection=False,
    )


def breakages(base: str, root: Path) -> tuple[list[str], str]:
    """Each change from ``base`` to the tree at ``root`` that breaks an extension, in griffe's words,
    and the ``PLUGIN_API_VERSION`` the base speaks."""

    with tempfile.TemporaryDirectory(prefix=BASE_PREFIX) as folder:
        before = Path(folder, "base")
        archive = subprocess.run(["git", "archive", base, "--", *SOURCES], cwd=root, check=True, capture_output=True)
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(before, filter="data")
        found = griffe.find_breaking_changes(load(before), load(root))
        lines = [_COLOR.sub("", breakage.explain(style=griffe.ExplanationStyle.ONE_LINE)) for breakage in found]
        return lines, plugin_api_version(before)


def released(root: Path) -> str | None:
    """The ``PLUGIN_API_VERSION`` released extensions speak: the default branch's where HEAD left
    it, or None where no default branch exists."""

    if not (released := released_fork(root)):
        return None
    ref, fork = released
    source = answered("show", f"{fork}:{PLUGINS.as_posix()}", root=root)
    return spoken(f"{ref}:{PLUGINS.as_posix()}", "\n".join(source)) if source else None


def main(argv: list[str]) -> int:
    base = merge_base(argv[0] if argv and argv[0] else None)
    print(f"lint:sdk-breaking: {PACKAGE} against {base[:9]}", flush=True)
    found, at_base = breakages(base, ROOT)
    current = plugin_api_version(ROOT)
    if not found:
        return 0
    print("\n".join(found), flush=True)
    moved = [version for version in (at_base, released(ROOT)) if version is not None and version != current]
    if moved:
        print(f"lint:sdk-breaking: {len(found)} breaks above, admitted by PLUGIN_API_VERSION {moved[0]} -> {current}")
        return 0
    print(
        f"lint:sdk-breaking: each of the {len(found)} changes above breaks an extension built against hq_sdk."
        " Keep the old shape, or move PLUGIN_API_VERSION in hq/platform/application/plugins.py"
        " and every extension's declared api_version with it (docs/Plugins.md).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
