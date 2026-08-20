"""Static conformance checks for plugin source trees."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

# These are implementation packages in the public host repository. Plugins get
# their supported equivalents from hq_sdk; importing one of these makes a host
# refactor a coordinated multi-repository migration.
#
# Written out by hand, this list drifted: `jobs` was added to the host and never
# added here, so a plugin importing `jobs.runner` was told the boundary passed.
# A check that silently stops checking is worse than no check, because the
# architecture test suite reports it as green.
#
# So the set is *derived* from the host tree and *floored* by this list. Union,
# never replacement: a new host app is caught the day it appears, and a host
# tree that cannot be read (an SDK installed without its host, a future
# packaging change) still enforces everything known at the time this shipped.
# The boundary can get stricter on its own. It cannot get weaker on its own.
_FLOOR = frozenset(
    {
        "application",
        "assets",
        "config",
        "contacts",
        "content",
        "control_plane",
        "controller_runtime",
        "core",
        "docs_index",
        "example_hq_plugin",
        "expenses",
        "hq_api",
        "hq_mcp",
        "jobs",
        "projects",
        "receipts",
        "reports",
        "search_index",
    }
)

# hq_sdk is the supported surface; it is the one host package a plugin may name.
_SUPPORTED_FACADE = "hq_sdk"


def _host_packages() -> frozenset[str]:
    """Top-level packages of the host this SDK was installed from."""

    root = Path(__file__).resolve().parent.parent
    try:
        entries = list(root.iterdir())
    except OSError:
        return frozenset()
    return frozenset(
        entry.name
        for entry in entries
        if entry.is_dir()
        and (entry / "__init__.py").exists()
        and entry.name != _SUPPORTED_FACADE
    )


HOST_INTERNAL_PACKAGES = _FLOOR | _host_packages()


def unsupported_hq_imports(source_root: str | Path) -> list[str]:
    """Return stable ``path:line: module`` violations for host-internal imports."""

    root = Path(source_root).resolve()
    violations: list[str] = []
    for source_path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(source_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as exc:
            violations.append(f"{source_path.relative_to(root)}:1: {exc}")
            continue
        for node in ast.walk(tree):
            modules: tuple[str, ...] = ()
            if isinstance(node, ast.ImportFrom) and node.module:
                modules = (node.module,)
            elif isinstance(node, ast.Import):
                modules = tuple(alias.name for alias in node.names)
            for module in modules:
                if module.partition(".")[0] in HOST_INTERNAL_PACKAGES:
                    violations.append(
                        f"{source_path.relative_to(root)}:{node.lineno}: {module}"
                    )
    return sorted(set(violations))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reject plugin imports outside HQ's supported hq_sdk facade."
    )
    parser.add_argument("source_root", type=Path)
    args = parser.parse_args()
    violations = unsupported_hq_imports(args.source_root)
    if violations:
        print("Unsupported HQ implementation imports; use hq_sdk instead:")
        for violation in violations:
            print(f"- {violation}")
        return 1
    print("HQ SDK import boundary passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
