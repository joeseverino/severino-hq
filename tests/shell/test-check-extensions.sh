#!/usr/bin/env bash
# Prove a pull request's host is checked against every extension, each from a
# copy of its package, that one failure fails the step only after every
# extension ran, and that the public log names none of them.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
checkout="$work/checkout"
mkdir -p "$bin" "$checkout/mise-tasks/plugin"

# docker run: reads the mounts and the package it was handed, prints what an
# extension's check would (its repository), leaves a mark in its copy, and
# fails for a repository in TEST_FAILING. Every call is logged.
cat >"$bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"$TEST_CALLS"
host="" src="" repository="" root=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --volume) case "$2" in *:/host) host=${2%:/host} ;; *:/src) src=${2%:/src} ;; esac; shift ;;
    --env) case "$2" in PLUGIN_REPOSITORY=*) repository=${2#*=} ;; esac; shift ;;
    --plugin-root) root=$2; shift ;;
  esac
  shift
done
[ -f "$host/mise-tasks/plugin/check" ] || { echo "no host checker mounted" >&2; exit 3; }
[ -f "$src/${root#/src/}/pyproject.toml" ] || { echo "no package at $root" >&2; exit 3; }
touch "$src/checked"
echo "checking $repository"
case " $TEST_FAILING " in *" $repository "*) exit 1 ;; esac
STUB
chmod +x "$bin/docker"

printf '#!/bin/sh\n' >"$checkout/mise-tasks/plugin/check"
git -C "$checkout" init -q
git -C "$checkout" add .
git -C "$checkout" -c user.name=example -c user.email=ci@example.com -c commit.gpgsign=false \
  commit --no-verify -qm fixture
for package in extensions/extensions/alpha extensions/extensions/beta extensions/gamma; do
  mkdir -p "$checkout/$package"
  printf '[project]\nname = "example"\n' >"$checkout/$package/pyproject.toml"
done
printf '%s' '{"extensions": [
  {"id": "example.alpha", "repository": "example/extensions", "path": "alpha"},
  {"id": "example.beta", "repository": "example/extensions", "path": "beta"},
  {"id": "example.gamma", "repository": "example/gamma"}]}' >"$checkout/set.json"

check() {
  : >"$work/calls"
  rm -rf "$work/temp" "$work/withheld"
  mkdir -p "$work/temp"
  (cd "$checkout" && PATH="$bin:$PATH" TEST_CALLS="$work/calls" TEST_FAILING="${FAILING-}" \
    RUNNER_TEMP="$work/temp" WITHHELD_DIR="$work/withheld" \
    "$repo_dir/tools/pipeline/check-extensions.sh" set.json example-installer:test) >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }
quiet() {
  for name in example/extensions example/gamma example.alpha example.gamma; do
    if grep -q "$name" "$work/out"; then fail "$1: the public log names $name"; fi
  done
}

check || fail "extensions that pass their checks failed the step"
[ "$(grep -c 'example-installer:test' "$work/calls")" -eq 3 ] || fail "not every extension was checked in the installer"
grep -q -- '--plugin-root /src/\.$' "$work/calls" || fail "an extension without a path was not checked from its root"
for id in example.alpha example.beta example.gamma; do
  [ -s "$work/withheld/extension-check-$id.log" ] || fail "$id's check output was not withheld"
done
[ ! -e "$checkout/extensions/gamma/checked" ] || fail "a check ran in the checked-out extension, not a copy"
quiet "passing checks"

if FAILING="example/extensions" check; then fail "an extension that fails its checks passed the step"; fi
[ "$(grep -c 'example-installer:test' "$work/calls")" -eq 3 ] || fail "a failure stopped the other extensions being checked"
grep -q '::error title=An extension fails this host::2 of 3' "$work/out" || fail "the failure does not count what failed"
grep -q 'example/extensions' "$work/withheld/extension-check-example.alpha.log" ||
  fail "the withheld log does not hold the failing check's output"
quiet "a failing check"

printf '%s' '{"extensions": []}' >"$checkout/set.json"
check || fail "a composition of no extensions failed its checks"
[ ! -s "$work/calls" ] || fail "a composition of no extensions ran a check"
echo "extension check tests passed"
