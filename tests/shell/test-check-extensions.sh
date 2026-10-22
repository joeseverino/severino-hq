#!/usr/bin/env bash
# Prove a pull request's host is checked against every extension, each from a
# copy of its package, the checks running at once, that one
# failure fails the step only after every extension ran, that the counts come
# in the set's order, and that the public log names none of them.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
checkout="$work/checkout"
mkdir -p "$bin" "$checkout/tools/pipeline"

# docker run: reads the mounts and the package it was handed, prints what an
# extension's check would (its repository), leaves a mark in its copy, and
# fails for a repository in TEST_FAILING. Every call is logged. The check
# named by TEST_WAITS finishes only once the one named by TEST_SIGNALS has
# started, which a run one at a time never sees: the wait gives up after 20
# seconds and fails the check.
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
check=${root#/src/}
[ "$check" != . ] || check=${repository##*/}
[ -f "$host/tools/pipeline/check-plugin.sh" ] || { echo "no host checker mounted" >&2; exit 3; }
[ -f "$src/${root#/src/}/pyproject.toml" ] || { echo "no package at $root" >&2; exit 3; }
touch "$src/checked"
echo "checking $repository"
if [ "$check" = "${TEST_SIGNALS-}" ]; then echo go >"$TEST_GO"; fi
if [ "$check" = "${TEST_WAITS-}" ]; then
  read -r -t 20 _ < <(cat "$TEST_GO") || { echo "$check ran alone"; exit 4; }
  echo "$check ran beside ${TEST_SIGNALS-}"
fi
case " $TEST_FAILING " in *" $repository "*) exit 1 ;; esac
STUB
chmod +x "$bin/docker"
# Room for every check at once, whatever runs the test.
printf '#!/bin/sh\necho 4\n' >"$bin/getconf"
chmod +x "$bin/getconf"

printf '#!/bin/sh\n' >"$checkout/tools/pipeline/check-plugin.sh"
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
  rm -rf "$work/temp" "$work/withheld" "$work/go"
  mkdir -p "$work/temp"
  mkfifo "$work/go"
  (cd "$checkout" && PATH="$bin:$PATH" TEST_CALLS="$work/calls" TEST_GO="$work/go" TEST_FAILING="${FAILING-}" \
    TEST_WAITS="${WAITS-}" TEST_SIGNALS="${SIGNALS-}" \
    RUNNER_TEMP="$work/temp" WITHHELD_DIR="$work/withheld" \
    "$repo_dir/tools/pipeline/check-extensions.sh" set.json example-installer:test) >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }
quiet() {
  for name in example/extensions example/gamma example.alpha example.gamma; do
    if grep -q "$name" "$work/out"; then fail "$1: the public log names $name"; fi
  done
}
counts() { grep '^extension ' "$work/out" | tr '\n' ' '; }

# The first extension finishes only after the last has started, so it is
# checked beside it and reported before it.
WAITS=alpha SIGNALS=gamma check || fail "extensions that pass their checks failed the step"
[ "$(grep -c 'example-installer:test' "$work/calls")" -eq 3 ] || fail "not every extension was checked in the installer"
grep -q -- '--plugin-root /src/\.$' "$work/calls" || fail "an extension without a path was not checked from its root"
[ "$(counts)" = "extension 1: passed extension 2: passed extension 3: passed " ] ||
  fail "the counts are not in the set's order"
for id in example.alpha example.beta example.gamma; do
  [ -s "$work/withheld/extension-check-$id.log" ] || fail "$id's check output was not withheld"
done
grep -q 'alpha ran beside gamma' "$work/withheld/extension-check-example.alpha.log" ||
  fail "the checks did not run at once"
for id in example.beta example.gamma; do
  if grep -q 'alpha' "$work/withheld/extension-check-$id.log"; then fail "$id's withheld log holds another check's output"; fi
done
[ ! -e "$checkout/extensions/gamma/checked" ] || fail "a check ran in the checked-out extension, not a copy"
quiet "passing checks"

if FAILING="example/gamma" WAITS=alpha SIGNALS=gamma check; then fail "an extension that fails its checks passed the step"; fi
[ "$(counts)" = "extension 1: passed extension 2: passed extension 3: failed " ] ||
  fail "the counts do not say which extension failed"
grep -q '::error title=An extension fails this host::1 of 3' "$work/out" || fail "the failure does not count what failed"
quiet "a failing check"

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
