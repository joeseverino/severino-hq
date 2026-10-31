#!/usr/bin/env bash
# Prove an extension's check refuses a package that declares no type check,
# and goes on to its environment for one that does, and that it ends on the
# gate's verdict, kept whole under the host's var/gate.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(CDPATH='' cd -- "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/bin" "$work/package/src/example_plugin"

# A host of only what the check runs, so its verdict lands in the fixture's var/gate.
host="$work/host"
mkdir -p "$host/tools/pipeline" "$host/tools/gate" "$host/hq/platform/application"
cp "$repo_dir/hq/platform/application/plugins.py" "$host/hq/platform/application/"
cp "$repo_dir/tools/__init__.py" "$host/tools/"
cp "$repo_dir/tools/pipeline/__init__.py" "$repo_dir/tools/pipeline/check-plugin.sh" \
  "$repo_dir/tools/pipeline/plugin_identity.py" "$host/tools/pipeline/"
cp "$repo_dir/tools/gate/verdict.py" "$host/tools/gate/"

# uv: marks that the check reached the package's environment, and goes no further.
printf '#!/bin/sh\ntouch "%s/synced"\nexit 3\n' "$work" >"$work/bin/uv"
chmod +x "$work/bin/uv"
printf '[project]\nname = "example-plugin"\nversion = "1.0.0"\n' >"$work/package/pyproject.toml"
printf 'plugin = PluginManifest(id="example.plugin", distribution="example-plugin", django_apps=("example_plugin",))\n' \
  >"$work/package/src/example_plugin/plugin.py"

check() {
  rm -f "$work/synced"
  PATH="$work/bin:$PATH" "$host/tools/pipeline/check-plugin.sh" --plugin-root "$work/package" >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }

if check; then fail "a package without a type check passed"; fi
grep -q 'declares no \[tool.mypy\]' "$work/out" || fail "the refusal does not say to declare a type check"
[ ! -e "$work/synced" ] || fail "a package without a type check was synced before it was refused"

kept="$host/var/gate/plugin-check-example_plugin.txt"
[ -f "$kept" ] || fail "the check kept no verdict in the host's var/gate"
last=$(tail -n 1 "$work/out")
case "$last" in "[plugin-check:example_plugin] FAILED"*) ;; *) fail "the check does not end on its verdict" ;; esac
grep -qF "verdict kept in $kept" "$work/out" || fail "the output does not name the file that keeps the verdict"
[ "$(head -n 1 "$kept")" = "$last" ] || fail "the kept verdict is not the closing line"

printf '\n[tool.mypy]\nstrict = true\n' >>"$work/package/pyproject.toml"
if check; then fail "the stub environment passed"; fi
[ -e "$work/synced" ] || fail "a package that declares its type check was refused"
echo "plugin check tests passed"
