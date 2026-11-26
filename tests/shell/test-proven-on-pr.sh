#!/usr/bin/env bash
# Prove a push is promoted only on full proof, and checked in full otherwise,
# and stands on its pull request's gates only when that run left none out.
# Proof covers the host image and the automation worker's image alike, and a
# release's composition only when its pull request's run recorded one of the
# same tree and identity, signed by its Composition job.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

tree="0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f"
commit="fedcba9876543210fedcba9876543210fedcba98"
head_sha="0123456789abcdef0123456789abcdef01234567"
digest="sha256:$(printf 'a%.0s' $(seq 64))"
runner_digest="sha256:$(printf 'b%.0s' $(seq 64))"
composed="ghcr.io/example/hq/composition@sha256:$(printf 'c%.0s' $(seq 64))"

cat >"$bin/gh" <<'STUB'
#!/bin/sh
case "$2" in
  */pulls) [ -n "$TEST_PR" ] && echo "$TEST_PR $TEST_HEAD" || echo "null null" ;;
  */git/commits/*) echo "$TEST_COMMIT_TREE" ;;
  */runs/*/jobs*) [ -z "$TEST_SKIPPED" ] || echo "$TEST_SKIPPED" ;;
  */runs*) echo "42 $TEST_CONCLUSION" ;;
esac
# gh run 42 --repo R --name composition --dir DIR: the run's record.
if [ "$1" = run ] && [ "$2" = download ]; then
  [ -n "$TEST_RECORD" ] || exit 1
  [ "$3" = 42 ] || exit 1
  echo "$TEST_RECORD" >"$9/verified-composition"
fi
STUB
# The worker's image is ghcr.io/example/hq/runner; each answer is its own.
cat >"$bin/docker" <<'STUB'
#!/bin/sh
case "$*" in
  */runner*Manifest.Digest*) [ -n "$TEST_RUNNER_DIGEST" ] && echo "$TEST_RUNNER_DIGEST" || exit 1 ;;
  *Manifest.Digest*) [ -n "$TEST_DIGEST" ] && echo "$TEST_DIGEST" || exit 1 ;;
  */composition@*.Image*) printf '{"config":{"Labels":{"dev.severino.hq.tree":"%s","dev.severino.hq.composition":"%s"}}}\n' "$TEST_TREE" "$TEST_IDENTITY" ;;
  */runner*.Image*) printf '{"config":{"Labels":{"dev.severino.hq.tree":"%s"}}}\n' "$TEST_RUNNER_TREE" ;;
  *.Image*) printf '{"config":{"Labels":{"dev.severino.hq.tree":"%s"}}}\n' "$TEST_TREE" ;;
esac
STUB
cat >"$bin/cosign" <<'STUB'
#!/bin/sh
signed=$TEST_SIGNED_PR workflow=ci.yml
case "$*" in
  */runner@*) signed=$TEST_RUNNER_SIGNED_PR ;;
  */composition@*) signed=$TEST_COMPOSED_SIGNED_PR workflow=$TEST_COMPOSED_SIGNER ;;
esac
for argument do
  case "$argument" in *"/workflows/$workflow@refs/pull/$signed/merge") exit 0 ;; esac
done
exit 1
STUB
chmod +x "$bin/gh" "$bin/docker" "$bin/cosign"

proof() {
  (cd "$repo_dir" && PATH="$bin:$PATH" GITHUB_REPOSITORY=example/hq GITHUB_SHA="${SHA-$commit}" \
    TEST_PR="${PR-176}" TEST_HEAD="$head_sha" TEST_CONCLUSION="${CONCLUSION-success}" \
    TEST_COMMIT_TREE="${COMMIT_TREE-$tree}" \
    TEST_RECORD="${RECORD-$composed}" TEST_IDENTITY="${IDENTITY-0123abcd}" \
    TEST_COMPOSED_SIGNED_PR="${COMPOSED_SIGNED-176}" TEST_COMPOSED_SIGNER="${COMPOSED_SIGNER-compose.yml}" \
    COMPOSITION="${COMPOSITION-}" \
    TEST_DIGEST="${DIGEST-$digest}" TEST_TREE="${TREE-$tree}" TEST_SIGNED_PR="${SIGNED-176}" \
    TEST_RUNNER_DIGEST="${RUNNER_DIGEST-$runner_digest}" TEST_RUNNER_TREE="${RUNNER_TREE-$tree}" \
    TEST_RUNNER_SIGNED_PR="${RUNNER_SIGNED-176}" \
    TEST_SKIPPED="${SKIPPED-}" \
    tools/pipeline/proven-on-pr.sh 2>/dev/null)
}
expect_unproven() {
  if [ -n "$(proof)" ]; then echo "promoted without proof: $1" >&2; exit 1; fi
}

[ "$(proof)" = "pr=176
digest=ghcr.io/example/hq@$digest
runner=ghcr.io/example/hq/runner@$runner_digest
gates=passed" ] || { echo "full proof was not promoted" >&2; exit 1; }
[ "$(SKIPPED=Browser proof)" = "pr=176
digest=ghcr.io/example/hq@$digest
runner=ghcr.io/example/hq/runner@$runner_digest" ] || { echo "a pull request that left out a gate stood for the push" >&2; exit 1; }
PR="" expect_unproven "no merged pull request"
CONCLUSION=failure expect_unproven "the pull request's CI failed"
CONCLUSION="" expect_unproven "the pull request's CI has not finished"
DIGEST="" expect_unproven "no image in the registry"
TREE=0000000000000000000000000000000000000000 expect_unproven "a different tree"
SIGNED=999 expect_unproven "signed by another pull request's run"
RUNNER_DIGEST="" expect_unproven "no worker image in the registry"
RUNNER_TREE=0000000000000000000000000000000000000000 expect_unproven "a worker image from a different tree"
RUNNER_SIGNED=999 expect_unproven "a worker image signed by another pull request's run"
COMMIT_TREE="" expect_unproven "a commit whose tree GitHub does not say"
SHA=ffff expect_unproven "a commit that is not a full SHA"

# A release's composition: its line only with every part of the proof, and
# the rest proven without it.
promoted() { COMPOSITION=0123abcd proof | sed -n 's/^composition=//p'; }
[ "$(promoted)" = "$composed" ] || { echo "a fully proven composition was not promoted" >&2; exit 1; }
[ "$(proof | grep -c '^composition=')" = 0 ] || { echo "a push that asked for no composition was handed one" >&2; exit 1; }
composed_unproven() {
  [ -z "$(promoted)" ] || { echo "a composition promoted without proof: $1" >&2; exit 1; }
  [ "$(COMPOSITION=0123abcd proof | sed -n 's/^pr=//p')" = 176 ] || { echo "the images went unproven with the composition: $1" >&2; exit 1; }
}
RECORD="" composed_unproven "its run recorded none"
RECORD="ghcr.io/example/hq/composition:pr-$head_sha" composed_unproven "the record names a tag"
IDENTITY=4567efab composed_unproven "other images or extensions"
COMPOSED_SIGNED=999 composed_unproven "signed for another pull request"
COMPOSED_SIGNER=ci.yml composed_unproven "signed by another workflow than Compose"
echo "proven-on-pr promotion tests passed"
