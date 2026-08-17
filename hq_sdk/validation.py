"""Static conformance checks for plugin source trees."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

# These are implementation packages in the public host repository. Plugins get
# their supported equivalents from hq_sdk; importing one of these makes a host
# refactor a coordinated multi-repository migration.
HOST_INTERNAL_PACKAGES = frozenset(
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
        "expenses",
        "hq_api",
        "hq_mcp",
        "projects",
        "receipts",
        "reports",
        "search_index",
    }
)


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
