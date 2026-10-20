#!/usr/bin/env bash
# Prove a composition's fingerprint follows its host image, its admissions and
# its policy, and that only an admission's dispatch skips a published one.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# docker buildx imagetools inspect TAG: published when TAG is in $TEST_PUBLISHED.
cat >"$bin/docker" <<'STUB'
#!/usr/bin/env bash
[ "$*" = "buildx imagetools inspect $4" ] || { echo "unexpected: $*" >&2; exit 3; }
[ -f "$TEST_PUBLISHED" ] && grep -qxF "$4" "$TEST_PUBLISHED"
STUB
chmod +x "$bin/docker"

host="ghcr.io/example/host@sha256:$(printf 'a%.0s' $(seq 64))"
printf 'example.alpha\texample/extensions\t200\n' >"$work/admissions.txt"
printf '{"version": 1}\n' >"$work/policy.json"

fingerprint() {
  : >"$work/github-output"
  PATH="$bin:$PATH" GITHUB_OUTPUT="$work/github-output" GITHUB_REPOSITORY=example/host \
    TEST_PUBLISHED="$work/published" FORCE="${FORCE-true}" \
    "$repo_dir/tools/pipeline/composition-fingerprint.sh" "${1:-$host}" "$work/admissions.txt" "$work/policy.json" \
    >"$work/out" 2>&1
}
output() { sed -n "s/^$1=//p" "$work/github-output"; }
fail() { echo "$1" >&2; cat "$work/out" "$work/github-output" >&2; exit 1; }

fingerprint || fail "a new composition was not fingerprinted"
first=$(output fingerprint)
[[ "$first" =~ ^[0-9a-f]{32}$ ]] || fail "the fingerprint is not 32 hex digits"
[ "$(output tag)" = "ghcr.io/example/host/composition:fp-$first" ] || fail "the tag is not the fingerprint's"
[ "$(output changed)" = true ] && [ "$(output build)" = true ] || fail "an unpublished composition was not built"

fingerprint && [ "$(output fingerprint)" = "$first" ] || fail "the same inputs gave another fingerprint"
fingerprint "${host%a}b" && [ "$(output fingerprint)" != "$first" ] || fail "another host image kept the fingerprint"
printf 'example.alpha\texample/extensions\t201\n' >"$work/admissions.txt"
fingerprint && [ "$(output fingerprint)" != "$first" ] || fail "a new admission kept the fingerprint"
printf 'example.alpha\texample/extensions\t200\n' >"$work/admissions.txt"
printf '{"version": 2}\n' >"$work/policy.json"
fingerprint && [ "$(output fingerprint)" != "$first" ] || fail "another policy kept the fingerprint"
printf '{"version": 1}\n' >"$work/policy.json"

echo "ghcr.io/example/host/composition:fp-$first" >"$work/published"
FORCE=false fingerprint || fail "a published composition was not fingerprinted"
[ "$(output changed)" = false ] && [ "$(output build)" = false ] || fail "an admission rebuilt a published composition"
FORCE=true fingerprint && [ "$(output changed)" = false ] && [ "$(output build)" = true ] ||
  fail "a forced rebuild of a published composition did not build"
echo "composition fingerprint tests passed"
