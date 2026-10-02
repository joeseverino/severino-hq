#!/usr/bin/env bash
# Whether the pull request this push to main merged already proved its tree.
#   scripts/proven-on-pr.sh >> "$GITHUB_OUTPUT"
# Prints `pr=` and `digest=` when the merged pull request's last CI run passed
# (every gate, its composition and CodeQL: Ready waits for all three) and its
# image was built from this exact tree and signed by that pull request's run.
# Otherwise it prints nothing and says why on stderr, and the push is checked
# in full. A squash merge of a branch that was current with main is that tree;
# anything else, a main that moved underneath it included, is not.
#
# The workflow that built the image is part of the tree compared, so what ran
# on the pull request is what was merged. Needs GH_TOKEN, a registry login and
# cosign. Run from the checkout of the pushed commit.
set -euo pipefail

repo="$GITHUB_REPOSITORY"
not_proven() { echo "checked in full: $1" >&2; exit 0; }

merged="$(gh api "repos/$repo/commits/$GITHUB_SHA/pulls" \
  --jq '[.[] | select(.merged_at != null and .base.ref == "main")][0] | "\(.number) \(.head.sha)"' 2>/dev/null || true)"
read -r pr head <<<"$merged" || true
[[ "${pr:-}" =~ ^[0-9]+$ && "${head:-}" =~ ^[0-9a-f]{40}$ ]] || not_proven "no merged pull request carries this commit"

conclusion="$(gh api "repos/$repo/actions/workflows/ci.yml/runs?event=pull_request&head_sha=$head&per_page=1" \
  --jq '.workflow_runs[0].conclusion // ""' 2>/dev/null || true)"
[ "$conclusion" = success ] || not_proven "#$pr's CI did not pass on its last commit"

image="ghcr.io/$repo:sha-${head::12}"
digest="$(docker buildx imagetools inspect "$image" --format '{{json .Manifest.Digest}}' 2>/dev/null | tr -d '"' || true)"
[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || not_proven "#$pr's image is not in the registry"
pinned="ghcr.io/$repo@$digest"

built="$(docker buildx imagetools inspect "$pinned" --format '{{json .Image}}' 2>/dev/null \
  | jq -r '.config.Labels["dev.severino.hq.tree"] // ""' || true)"
[ "$built" = "$(git rev-parse 'HEAD^{tree}')" ] || not_proven "#$pr's image was built from a different tree"

cosign verify \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity "https://github.com/$repo/.github/workflows/ci.yml@refs/pull/$pr/merge" \
  "$pinned" >/dev/null 2>&1 || not_proven "#$pr's image is not signed by its own CI run"

echo "pr=$pr"
echo "digest=$pinned"
