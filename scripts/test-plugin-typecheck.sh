#!/usr/bin/env bash
# Prove an extension's declared type check runs against the host's hq_sdk with
# the package installed, fails on a type error, and is skipped when undeclared.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
python="$(uv run --locked --project "$repo_dir" python -c 'import sys; print(sys.executable)')"

export DJANGO_DEBUG=true
export SEVERINO_DATABASE_PATH="$work/check.sqlite3"
export SEVERINO_HQ_PLUGINS=tests.fixtures.example_hq_plugin.plugin:plugin

# A package whose module reads the installed example plugin through hq_sdk's types.
package() {
  local root="$work/$1"
  mkdir -p "$root/src/example_typed"
  printf '[project]\nname = "example-typed"\nversion = "1.0.0"\n' > "$root/pyproject.toml"
  printf 'from hq_sdk.plugin import PluginManifest\nfrom tests.fixtures.example_hq_plugin.plugin import plugin\n\nmanifest: PluginManifest = plugin\n' \
    > "$root/src/example_typed/__init__.py"
  echo "$root"
}
declare_mypy() {
  cat >> "$1/pyproject.toml" <<'TOML'

[tool.mypy]
strict = true
plugins = ["mypy_django_plugin.main"]
files = ["src"]
mypy_path = "src"
explicit_package_bases = true
follow_imports = "silent"

[tool.django-stubs]
django_settings_module = "hq.config.settings_typecheck"
TOML
}
typecheck() {
  (cd "$1" && "$repo_dir/scripts/plugin-typecheck.sh" "$python")
}

typed="$(package typed)"
declare_mypy "$typed"
typecheck "$typed" >/dev/null || { echo "a well-typed package failed its type check" >&2; exit 1; }

broken="$(package broken)"
declare_mypy "$broken"
printf 'name: int = plugin.name\n' >> "$broken/src/example_typed/__init__.py"
if output="$(typecheck "$broken")"; then
  echo "a type error passed the type check" >&2; exit 1
fi
grep -q 'assignment' <<<"$output" || { echo "the failure is not the type error: $output" >&2; exit 1; }

undeclared="$(package undeclared)"
printf 'name: int = plugin.name\n' >> "$undeclared/src/example_typed/__init__.py"
typecheck "$undeclared" | grep -q 'no \[tool.mypy\]' || { echo "an undeclared type check was not skipped" >&2; exit 1; }

echo "plugin type check: ok"
