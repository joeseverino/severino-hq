#!/usr/bin/env bash
# The extension repositories that carry a pull request's branch.
#   tools/pipeline/coordinated-branches.sh SET BRANCH OUT   # OUT: repository=sha, one line per repository
# SET is the composition set. A repository carrying BRANCH is a coordinated
# change: Compose builds its extensions from that commit instead of reusing
# their admission. Writes found=true|false to $GITHUB_OUTPUT. Needs GH_TOKEN
# able to read the extension repositories.
set -euo pipefail

[ "$#" -eq 3 ] || { echo "usage: $0 SET BRANCH OUT" >&2; exit 2; }
set_file=$1 branch=$2 out=$3

if [[ ! "$branch" =~ ^[A-Za-z0-9._/-]+$ ]]; then
  echo "::error::The pull request branch name has characters this step does not accept."
  exit 1
fi

error=$(mktemp)
trap 'rm -f "$error"' EXIT
: >"$out"
while read -r repository; do
  if sha=$(gh api "repos/$repository/branches/$branch" --jq .commit.sha 2>"$error"); then
    printf '%s=%s\n' "$repository" "$sha" >>"$out"
  elif ! grep -q 'HTTP 404' "$error"; then
    # Not "no such branch": the answer is unknown, and guessing either way
    # would verify the wrong composition.
    echo "::error::Could not ask an extension repository for branch $branch."
    exit 1
  fi
done < <(jq -r '.extensions[].repository' "$set_file" | sort -u)

count=$(grep -c . "$out" || true)
echo "found=$([ "$count" -gt 0 ] && echo true || echo false)" >>"$GITHUB_OUTPUT"
echo "coordinated extension branches named $branch: $count"
