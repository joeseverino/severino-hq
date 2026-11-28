# shellcheck shell=bash
# The pull request a commit on main merged, and that commit's tree: the first
# half of every proof that a merge was already checked on its pull request.
# Sourced by tools/pipeline/proven-on-pr.sh (the host's merges) and
# tools/pipeline/checked-on-host.sh (an extension's), with COMMIT set; sets
# pr, head and tree, or says why not and exits 0, so the push is checked in
# full. Needs GH_TOKEN able to read GITHUB_REPOSITORY's pull requests.

repo="$GITHUB_REPOSITORY"
not_proven() { echo "checked in full: $1" >&2; exit 0; }

[[ "${commit:?the caller sets commit}" =~ ^[0-9a-f]{40}$ ]] || not_proven "the commit is not a full SHA"
merged="$(gh api "repos/$repo/commits/$commit/pulls" \
  --jq '[.[] | select(.merged_at != null and .base.ref == "main")][0] | "\(.number) \(.head.sha)"' 2>/dev/null || true)"
read -r pr head <<<"$merged" || true
[[ "${pr:-}" =~ ^[0-9]+$ && "${head:-}" =~ ^[0-9a-f]{40}$ ]] || not_proven "no merged pull request carries this commit"

tree="$(gh api "repos/$repo/git/commits/$commit" --jq .tree.sha 2>/dev/null || true)"
[[ "$tree" =~ ^[0-9a-f]{40}$ ]] || not_proven "the commit's tree could not be read"
