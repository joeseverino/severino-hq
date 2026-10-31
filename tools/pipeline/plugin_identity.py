"""Read an extension's identity from the package, where it is declared once.

`python3 -m tools.pipeline.plugin_identity [ROOT] [--repository OWNER/NAME]`
prints key=value lines for GITHUB_OUTPUT. It reads without importing, so neither
Django nor the host loads, and fails on any disagreement before a build or a
signature (docs/Plugins.md, The golden path).
"""

from __future__ import annotations

import argparse
import ast
import re
import tomllib
from pathlib import Path

MANIFEST = "PluginManifest"
HOST = Path(__file__).resolve().parents[2]
PLUGINS = Path("hq/platform/application/plugins.py")


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
            and [target.id if isinstance(target, ast.Name) else None for target in node.targets] == ["plugin"]
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == MANIFEST
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
    # Checked here, since the host refuses a wheel from another repository only
    # once it is composed.
    if repository is not None and (named := literal(call, "source_repository")) != repository:
        raise SystemExit(f"plugin.py: source_repository is {named!r}; this package is admitted from {repository!r}")
    return {
        "distribution": distribution,
        "plugin-id": str(literal(call, "id")),
        "plugin-reference": f"{package}.plugin:plugin",
        "django-app": package,
    }


def plugin_api_version(host: Path = HOST) -> str:
    """The PLUGIN_API_VERSION the host at ``host`` speaks, read without importing it."""
    match = re.search(r"^PLUGIN_API_VERSION\s*=\s*(\d+)\s*$", (host / PLUGINS).read_text(), re.MULTILINE)
    if not match:
        raise SystemExit(f"{PLUGINS} does not define PLUGIN_API_VERSION")
    return match.group(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("root", nargs="?", default=".", type=Path)
    parser.add_argument("--repository")
    arguments = parser.parse_args()
    for key, value in identity(arguments.root, arguments.repository).items():
        print(f"{key}={value}")
    # An admission records it: the host refuses a wheel whose approval names
    # another version than its manifest declares.
    print(f"plugin-api-version={plugin_api_version()}")
