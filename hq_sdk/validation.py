"""Static conformance checks for plugin source trees."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

# These are implementation packages in the public host repository. Plugins get
# their supported equivalents from hq_sdk; importing one of these makes a host
# refactor a coordinated multi-repository migration.
#
# A list written out by hand drifts: a new host app missing from it lets a
# plugin import that app while the boundary check passes. A check that silently
# stops checking is worse than no check, because the architecture test suite
# reports it as green.
#
# So the set is *derived* from the host tree and *floored* by this list. Union,
# never replacement: a new host app is caught the day it appears, and a host
# tree that cannot be read (an SDK installed without its host, a future
# packaging change) still enforces everything known at the time this shipped.
# The boundary can get stricter on its own. It cannot get weaker on its own.
_FLOOR = frozenset(
    {
        "analytics",
        "application",
        "assets",
        "calendars",
        "config",
        "contacts",
        "content",
        "control_plane",
        "core",
        "docs_index",
        "example_hq_plugin",
        "expenses",
        "hq",
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

    root = Path(__file__).resolve().parents[1]
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


def _imported_modules(node: ast.Import | ast.ImportFrom) -> tuple[str, ...]:
    """The absolute module names one import statement names."""

    if isinstance(node, ast.ImportFrom):
        # A relative import names the plugin's own package, never the host's,
        # however its module is spelled: `from .calendars import x` is the
        # plugin's own calendars.
        return (node.module,) if node.module and not node.level else ()
    return tuple(alias.name for alias in node.names)


def _file_violations(source_path: Path, root: Path) -> list[str]:
    """Each host-internal import in one file; an unreadable file is one itself."""

    relative = source_path.relative_to(root)
    try:
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        return [f"{relative}:1: {exc}"]
    return [
        f"{relative}:{node.lineno}: {module}"
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for module in _imported_modules(node)
        if module.partition(".")[0] in HOST_INTERNAL_PACKAGES
    ]


def unsupported_hq_imports(source_root: str | Path) -> list[str]:
    """Return stable ``path:line: module`` violations for host-internal imports."""

    root = Path(source_root).resolve()
    violations: list[str] = []
    for source_path in sorted(root.rglob("*.py")):
        violations.extend(_file_violations(source_path, root))
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
