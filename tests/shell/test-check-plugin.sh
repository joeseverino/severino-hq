#!/usr/bin/env bash
# Prove an extension's check refuses a package that declares no type check,
# and goes on to its environment for one that does.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/package/src/example_plugin"

# uv: marks that the check reached the package's environment, and goes no further.
printf '#!/bin/sh\ntouch "%s/synced"\nexit 3\n' "$work" >"$work/bin/uv"
chmod +x "$work/bin/uv"
printf '[project]\nname = "example-plugin"\nversion = "1.0.0"\n' >"$work/package/pyproject.toml"
printf 'plugin = PluginManifest(id="example.plugin", distribution="example-plugin", django_apps=("example_plugin",))\n' \
  >"$work/package/src/example_plugin/plugin.py"

check() {
  rm -f "$work/synced"
  PATH="$work/bin:$PATH" "$repo_dir/tools/pipeline/check-plugin.sh" --plugin-root "$work/package" >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }

if check; then fail "a package without a type check passed"; fi
grep -q 'declares no \[tool.mypy\]' "$work/out" || fail "the refusal does not say to declare a type check"
[ ! -e "$work/synced" ] || fail "a package without a type check was synced before it was refused"

printf '\n[tool.mypy]\nstrict = true\n' >>"$work/package/pyproject.toml"
if check; then fail "the stub environment passed"; fi
[ -e "$work/synced" ] || fail "a package that declares its type check was refused"
echo "plugin check tests passed"
