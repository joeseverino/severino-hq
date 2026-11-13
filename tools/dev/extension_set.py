"""Name the extension set in this checkout's mise.local.toml from the extension checkouts themselves.

Each package is read the way admission reads it (tools.pipeline.plugin_identity),
so the set named here is the one Compose builds. mise writes the file, which
keeps every other value already in it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from hq.platform.core.tests.task import usage
from tools.pipeline.plugin_identity import identity

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


def main() -> int:
    checkouts = [Path(each).expanduser() for each in usage().repeated("checkout")]
    named = extension_set(checkouts)
    write(LOCAL, named)
    print(f"{LOCAL.name} names {named['HQ_LOCAL_PLUGINS']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
