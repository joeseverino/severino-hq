#!/usr/bin/env bash
# Prove a pull request's extensions are checked out at their coordinated
# commit or their default branch with no .git, that only the extensions a
# coordinated branch carries at their listed path are built, and that the
# public log names none of them.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# gh repo clone REPOSITORY DIR: a checkout holding each package TEST_PACKAGES
# lists for it (name:id), a .git, and the repository named in a file. Fails
# for a repository in TEST_UNCLONABLE.
cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$TEST_CALLS"
repository=$3 dir=$4
case " $TEST_UNCLONABLE " in *" $repository "*) echo "clone of $repository refused" >&2; exit 1 ;; esac
mkdir -p "$dir/.git"
for entry in $TEST_PACKAGES; do
  [ "${entry%%:*}" = "$repository" ] || continue
  rest=${entry#*:} package=${rest%%:*} id=${rest#*:}
  name=example-${id#example.}
  mkdir -p "$dir/$package/src/${name//-/_}"
  printf '[project]\nname = "%s"\nversion = "1.0"\n' "$name" >"$dir/$package/pyproject.toml"
  printf 'from hq_sdk.plugin import PluginManifest\nplugin = PluginManifest(id="%s", distribution="%s", django_apps=("%s",))\n' \
    "$id" "$name" "${name//-/_}" >"$dir/$package/src/${name//-/_}/plugin.py"
done
STUB
cat >"$bin/git" <<'STUB'
#!/usr/bin/env bash
echo "git $*" >>"$TEST_CALLS"
STUB
# docker run … uv build … /src/<package>: a wheel in the /out mount, unless
# the package is in TEST_BROKEN.
cat >"$bin/docker" <<'STUB'
#!/usr/bin/env bash
echo "docker $*" >>"$TEST_CALLS"
out="" package="${*: -1}"
while [ "$#" -gt 0 ]; do
  case "$1" in --volume) case "$2" in *:/out) out=${2%:/out} ;; esac; shift ;; esac
  shift
done
case " $TEST_BROKEN " in *" ${package#/src/} "*) echo "building $package failed"; exit 1 ;; esac
touch "$out/${package##*/}-1.0-py3-none-any.whl"
STUB
chmod +x "$bin/gh" "$bin/git" "$bin/docker"

printf '%s' '{"extensions": [
  {"id": "example.alpha", "repository": "example/extensions", "path": "alpha"},
  {"id": "example.beta", "repository": "example/extensions", "path": "beta"},
  {"id": "example.gamma", "repository": "example/gamma"}]}' >"$work/set.json"
packages="example/extensions:alpha:example.alpha example/extensions:beta:example.beta example/gamma:.:example.gamma"
sha=$(printf 'c%.0s' $(seq 40))

branches() {
  rm -rf "$work/run"
  mkdir -p "$work/run"
  : >"$work/calls"
  (cd "$work/run" && PATH="$bin:$PATH" PYTHONPATH="$repo_dir" TEST_CALLS="$work/calls" WITHHELD_DIR="$work/withheld" \
    TEST_PACKAGES="${PACKAGES-$packages}" TEST_UNCLONABLE="${UNCLONABLE-}" TEST_BROKEN="${BROKEN-}" \
    "$repo_dir/tools/pipeline/extension-branches.sh" checkout ../set.json ../coordinated.txt &&
    PATH="$bin:$PATH" PYTHONPATH="$repo_dir" TEST_CALLS="$work/calls" WITHHELD_DIR="$work/withheld" \
    TEST_BROKEN="${BROKEN-}" \
    "$repo_dir/tools/pipeline/extension-branches.sh" build ../set.json ../coordinated.txt example-installer:test) \
    >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" "$work/calls" >&2; exit 1; }
quiet() {
  for name in example/extensions example/gamma example.alpha example.beta example.gamma; do
    if grep -q "$name" "$work/out"; then fail "$1: the public log names $name"; fi
  done
}

printf 'example/extensions=%s\n' "$sha" >"$work/coordinated.txt"
branches || fail "a coordinated branch did not check out and build"
{ [ "$(grep -c '^git ' "$work/calls")" -eq 1 ] &&
  grep -q "git -C extensions/extensions checkout --quiet --detach $sha" "$work/calls"; } ||
  fail "only the coordinated repository is not moved to its coordinated commit"
{ [ ! -e "$work/run/extensions/extensions/.git" ] && [ ! -e "$work/run/extensions/gamma/.git" ]; } ||
  fail "a checked-out extension kept its .git"
[ "$(cd "$work/run/candidate-wheels" && ls)" = "$(printf '%s\n' alpha-1.0-py3-none-any.whl beta-1.0-py3-none-any.whl)" ] ||
  fail "the coordinated repository's extensions were not each built from their path"
grep -q 'example-installer:test' "$work/calls" || fail "a branch was built outside the installer"
! grep -q -e 'GH_TOKEN' -e 'docker.sock' "$work/calls" || fail "a branch build was handed a credential"
quiet "a coordinated build"

if PACKAGES="example/extensions:alpha:example.alpha example/extensions:beta:example.delta example/gamma:.:example.gamma" branches; then
  fail "a branch carrying another extension at a listed path was built"
fi
grep -q '::error title=Coordinated branch did not build::' "$work/out" || fail "a wrong extension at a path did not fail with an error"
quiet "a wrong extension at a path"

if BROKEN="beta" branches; then fail "a branch that does not build was composed"; fi
grep -q 'building /src/beta failed' "$work/withheld/candidate-build-example.beta.log" || fail "a failed build's output was not withheld"
quiet "a failed build"

if UNCLONABLE="example/gamma" branches; then fail "an extension that could not be cloned was composed"; fi
grep -q '::error title=Extension not checked out::' "$work/out" || fail "a failed clone did not fail with an error"
grep -q 'example/gamma' "$work/withheld/clone-gamma.log" || fail "a failed clone's reason was not withheld"
quiet "a failed clone"

: >"$work/coordinated.txt"
branches || fail "extensions with no coordinated branch did not check out"
! grep -q -e '^git ' -e '^docker ' "$work/calls" || fail "an extension with no coordinated branch was moved or built"
echo "extension branch tests passed"
