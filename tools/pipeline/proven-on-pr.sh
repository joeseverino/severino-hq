#!/usr/bin/env bash
# Whether the pull request a commit on main merged already proved its tree.
#   tools/pipeline/proven-on-pr.sh [COMMIT] >> "$GITHUB_OUTPUT"
# COMMIT is the pushed commit, GITHUB_SHA by default. Prints `pr=`, `digest=`
# and `runner=` when the merged pull request's last CI run passed (every gate,
# its composition and CodeQL are jobs of that run) and its host image and the
# automation worker's image were each built from this exact tree and signed by
# that pull request's run, and `gates=passed` when that run left out none of
# the gates. Otherwise it prints nothing and says why on stderr, and the push
# is checked in full. A squash merge of a branch that was current with main is
# that tree; anything else, a main that moved underneath it included, is not.
#
# With COMPOSITION set to a composition's identity
# (tools/pipeline/composition-identity.sh), as a release computes it from the
# admissions it verified, it also prints `composition=` when that run's
# Composition job verified a composition of this tree with that identity: the
# digest the job recorded in its run's `composition` artifact, signed by the
# job for that pull request. A composition not proven leaves the rest proven.
#
# An extension's merge is proven by tools/pipeline/checked-on-host.sh, from
# the same merged pull request (tools/pipeline/merged-pr.sh).
#
# The workflow that built the image is part of the tree compared, so what ran
# on the pull request is what was merged. Needs GH_TOKEN able to read the
# repository's pull requests and runs, a registry login and cosign.
set -euo pipefail

commit="${1:-$GITHUB_SHA}"
# shellcheck source=tools/pipeline/merged-pr.sh
. "$(dirname -- "$0")/merged-pr.sh"

last="$(gh api "repos/$repo/actions/workflows/ci.yml/runs?event=pull_request&head_sha=$head&per_page=1" \
  --jq '.workflow_runs[0] | "\(.id) \(.conclusion // "")"' 2>/dev/null || true)"
read -r run conclusion <<<"$last" || true
[[ "${run:-}" =~ ^[0-9]+$ && "${conclusion:-}" = success ]] || not_proven "#$pr's CI did not pass on its last commit"

# Whether PINNED was built from this tree and signed by WORKFLOW for this pull request.
built_here() {
  local pinned=$1 workflow=$2 built
  built="$(docker buildx imagetools inspect "$pinned" --format '{{json .Image}}' 2>/dev/null \
    | jq -r '.config.Labels["dev.severino.hq.tree"] // ""' || true)"
  [ "$built" = "$tree" ] || { echo "built from a different tree"; return 1; }
  cosign verify \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity "https://github.com/$repo/.github/workflows/$workflow@refs/pull/$pr/merge" \
    "$pinned" >/dev/null 2>&1 || { echo "not signed by its own CI run"; return 1; }
}

# Sets pinned to the image NAME holds for the pull request's head, by digest,
# once it is proven built from this tree and signed by that pull request's run.
prove() {
  local name=$1 what=$2 digest why
  digest="$(docker buildx imagetools inspect "$name:sha-${head::12}" --format '{{.Manifest.Digest}}' 2>/dev/null || true)"
  [[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || not_proven "#$pr's $what is not in the registry"
  pinned="$name@$digest"
  why="$(built_here "$pinned" ci.yml)" || not_proven "#$pr's $what was $why"
}
prove "ghcr.io/$repo" image
image=$pinned
prove "ghcr.io/$repo/runner" "worker image"

echo "pr=$pr"
echo "digest=$image"
echo "runner=$pinned"

# A pull request runs only the legs its change reaches (tools/pipeline/legs.py),
# so its Lint, Test and Browser stand for this push only when it skipped none:
# otherwise the push runs every gate, and promotes the image all the same.
skipped="$(gh api "repos/$repo/actions/runs/$run/jobs?per_page=100" --paginate \
  --jq '.jobs[] | select(.conclusion == "skipped" and (.name | test("^(Lint|Test|Browser)( |$)"))) | .name' \
  2>/dev/null | paste -sd, -)" || skipped="its jobs, unread"
if [ -n "$skipped" ]; then
  echo "gates run here: #$pr left out $skipped" >&2
else
  echo "gates=passed"
fi

[ -n "${COMPOSITION:-}" ] || exit 0
composed() { echo "composed in full: $1" >&2; exit 0; }
# The run's own record of the digest its Composition job verified, never a
# tag: a run's artifacts are its own, and this is the run that passed.
record="$(mktemp -d)"
gh run download "$run" --repo "$repo" --name composition --dir "$record" >/dev/null 2>&1 ||
  composed "#$pr's CI recorded no verified composition"
verified="$(head -n 1 "$record/verified-composition" 2>/dev/null || true)"
[[ "$verified" =~ ^ghcr\.io/$repo/composition@sha256:[0-9a-f]{64}$ ]] ||
  composed "#$pr's record names no composition by digest"
why="$(built_here "$verified" compose.yml)" || composed "#$pr's composition was $why"
identity="$(docker buildx imagetools inspect "$verified" --format '{{json .Image}}' 2>/dev/null \
  | jq -r '.config.Labels["dev.severino.hq.composition"] // ""' || true)"
[ "$identity" = "$COMPOSITION" ] ||
  composed "#$pr composed other images or extensions than this release admits"
echo "composition=$verified"
