#!/usr/bin/env bash
# Prove the host image is composed only once this repository's ci workflow has
# signed it, from main alone in a release, and that a missing signature is
# waited for and then refused.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# cosign verify: passes when the identity regexp admits TEST_SIGNER and it has
# been asked more than TEST_SIGNED_AFTER times. Each ask is counted.
cat >"$bin/cosign" <<'STUB'
#!/usr/bin/env bash
echo verify >>"$TEST_CALLS"
while [ "$#" -gt 0 ]; do
  case "$1" in --certificate-identity-regexp) pattern=$2; shift ;; esac
  shift
done
[ "$(grep -c verify "$TEST_CALLS")" -gt "$TEST_SIGNED_AFTER" ] && printf '%s\n' "$TEST_SIGNER" | grep -Eq "$pattern"
STUB
cat >"$bin/sleep" <<'STUB'
#!/bin/sh
echo sleep >>"$TEST_CALLS"
STUB
chmod +x "$bin/cosign" "$bin/sleep"

main=https://github.com/example/host/.github/workflows/ci.yml@refs/heads/main
pull=https://github.com/example/host/.github/workflows/ci.yml@refs/pull/7/merge
signature() {
  : >"$work/calls"
  PATH="$bin:$PATH" TEST_CALLS="$work/calls" TEST_SIGNER="$1" TEST_SIGNED_AFTER="${SIGNED_AFTER-0}" \
    IS_RELEASE="${RELEASE-false}" \
    "$repo_dir/tools/pipeline/host-signature.sh" example/host "ghcr.io/example/host@sha256:$(printf 'd%.0s' $(seq 64))" \
    >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }

RELEASE=true signature "$main" || fail "a release refused a host built from main"
! grep -q sleep "$work/calls" || fail "a signed image was waited for"
if RELEASE=true signature "$pull"; then fail "a release composed onto a pull request's build"; fi
grep -q '::error title=Host image is not signed::' "$work/out" || fail "a refused signature did not say so"
[ "$(grep -c verify "$work/calls")" -eq 12 ] || fail "a missing signature was not waited for to its bound"
signature "$pull" || fail "a pull request's composition refused its own build"
if signature https://github.com/example/other/.github/workflows/ci.yml@refs/heads/main; then
  fail "another repository's signature was accepted"
fi
if signature https://github.com/example/host/.github/workflows/compose.yml@refs/heads/main; then
  fail "a signature by another workflow was accepted"
fi
SIGNED_AFTER=3 signature "$main" || fail "a signature that arrived after the push was not waited for"
echo "host signature tests passed"
