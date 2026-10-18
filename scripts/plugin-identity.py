#!/usr/bin/env python3
"""Read an extension's identity from the package, where it is declared once.

    plugin-identity.py [ROOT] [--repository OWNER/NAME]   # key=value lines for GITHUB_OUTPUT

The distribution is ``[project].name`` in ROOT/pyproject.toml; its package is
that name with underscores, at ``src/<package>``; the manifest is the
``plugin = PluginManifest(...)`` in that package's ``plugin.py``. Read without
importing, so it needs neither Django nor the host. The manifest's ``id``,
``distribution`` and ``django_apps`` must be literals, and its distribution
must be the project's: any mismatch fails here, before a build or a signature.
Given the repository it is checked or admitted in, the manifest's
``source_repository`` must name it, or the host refuses the admitted wheel
only once it is composed.
"""

from __future__ import annotations

import argparse
import ast
import tomllib
from pathlib import Path

MANIFEST = "PluginManifest"


def literal(call: ast.Call, name: str) -> object:
    for keyword in call.keywords:
        if keyword.arg == name:
            try:
                return ast.literal_eval(keyword.value)
            except ValueError:
                raise SystemExit(f"plugin.py: {name}= must be a literal") from None
    raise SystemExit(f"plugin.py: the manifest declares no {name}=")


def manifest(source: str) -> ast.Call:
    for node in ast.parse(source).body:
        if (
            isinstance(node, ast.Assign)
            and [getattr(target, "id", None) for target in node.targets] == ["plugin"]
            and isinstance(node.value, ast.Call)
            and getattr(node.value.func, "id", None) == MANIFEST
        ):
            return node.value
    raise SystemExit(f"plugin.py: no module-level `plugin = {MANIFEST}(...)`")


def identity(root: Path, repository: str | None = None) -> dict[str, str]:
    distribution = tomllib.loads((root / "pyproject.toml").read_text())["project"]["name"]
    package = distribution.replace("-", "_")
    source = root / "src" / package / "plugin.py"
    if not source.is_file():
        raise SystemExit(f"{distribution}: expected its manifest at src/{package}/plugin.py")
    call = manifest(source.read_text())
    declared = literal(call, "distribution")
    if declared != distribution:
        raise SystemExit(f"plugin.py declares distribution {declared!r}; pyproject.toml names {distribution!r}")
    apps = literal(call, "django_apps")
    if not isinstance(apps, tuple) or package not in apps:
        raise SystemExit(f"plugin.py: django_apps must include the package, {package!r}")
    if repository is not None and (named := literal(call, "source_repository")) != repository:
        raise SystemExit(f"plugin.py: source_repository is {named!r}; this package is admitted from {repository!r}")
    return {
        "distribution": distribution,
        "plugin-id": str(literal(call, "id")),
        "plugin-reference": f"{package}.plugin:plugin",
        "django-app": package,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("--repository")
    arguments = parser.parse_args()
    for key, value in identity(arguments.root, arguments.repository).items():
        print(f"{key}={value}")
