#!/usr/bin/env bash
# Prove a push is promoted only on full proof, and checked in full otherwise.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

tree="$(git -C "$repo_dir" rev-parse 'HEAD^{tree}')"
head_sha="0123456789abcdef0123456789abcdef01234567"
digest="sha256:$(printf 'a%.0s' $(seq 64))"

cat >"$bin/gh" <<'STUB'
#!/bin/sh
case "$2" in
  */pulls) [ -n "$TEST_PR" ] && echo "$TEST_PR $TEST_HEAD" || echo "null null" ;;
  */runs*) echo "$TEST_CONCLUSION" ;;
esac
STUB
cat >"$bin/docker" <<'STUB'
#!/bin/sh
case "$*" in
  *Manifest.Digest*) [ -n "$TEST_DIGEST" ] && echo "\"$TEST_DIGEST\"" || exit 1 ;;
  *.Image*) printf '{"config":{"Labels":{"dev.severino.hq.tree":"%s"}}}\n' "$TEST_TREE" ;;
esac
STUB
cat >"$bin/cosign" <<'STUB'
#!/bin/sh
for argument do
  case "$argument" in *"@refs/pull/$TEST_SIGNED_PR/merge") exit 0 ;; esac
done
exit 1
STUB
chmod +x "$bin/gh" "$bin/docker" "$bin/cosign"

proof() {
  (cd "$repo_dir" && PATH="$bin:$PATH" GITHUB_REPOSITORY=example/hq GITHUB_SHA=ffff \
    TEST_PR="${PR-176}" TEST_HEAD="$head_sha" TEST_CONCLUSION="${CONCLUSION-success}" \
    TEST_DIGEST="${DIGEST-$digest}" TEST_TREE="${TREE-$tree}" TEST_SIGNED_PR="${SIGNED-176}" \
    scripts/proven-on-pr.sh 2>/dev/null)
}
expect_unproven() {
  if [ -n "$(proof)" ]; then echo "promoted without proof: $1" >&2; exit 1; fi
}

[ "$(proof)" = "pr=176
digest=ghcr.io/example/hq@$digest" ] || { echo "full proof was not promoted" >&2; exit 1; }
PR="" expect_unproven "no merged pull request"
CONCLUSION=failure expect_unproven "the pull request's CI failed"
CONCLUSION="" expect_unproven "the pull request's CI has not finished"
DIGEST="" expect_unproven "no image in the registry"
TREE=0000000000000000000000000000000000000000 expect_unproven "a different tree"
SIGNED=999 expect_unproven "signed by another pull request's run"
echo "proven-on-pr promotion tests passed"
