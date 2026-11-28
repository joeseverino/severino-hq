#!/usr/bin/env bash
# The extension repositories a check builds from a branch rather than as
# admitted.
#   tools/pipeline/coordinated-branches.sh SET BRANCH OUT          # a host pull request's branch
#   tools/pipeline/coordinated-branches.sh SET --commit SHA OUT    # an extension pull request's head
# OUT: repository=sha, one line per repository. SET is the composition set.
#
# A repository carrying BRANCH is a coordinated change: its extensions are
# built from that commit instead of reusing their admission. With --commit,
# the one repository with a pull request whose head is SHA is the change, its
# open one first, else the one that merged or closed while the check ran, so
# every job of a run answers alike whenever it asks. Its base commit is
# written as base= for the secret scan of what the pull request brings, and
# its name, which the set's mask hides, as name= for the token that reports
# on it. Writes found=true|false to $GITHUB_OUTPUT. Needs
# GH_TOKEN able to read the extension repositories and their pull requests.
set -euo pipefail

usage() { echo "usage: $0 SET BRANCH OUT | SET --commit SHA OUT" >&2; exit 2; }
error=$(mktemp)
trap 'rm -f "$error"' EXIT
# Not "absent": the answer is unknown, and guessing either way would verify
# the wrong composition.
unanswered() {
  if ! grep -q -e 'HTTP 404' -e 'HTTP 422' "$error"; then
    echo "::error::Could not ask an extension repository about $1."
    exit 1
  fi
}
repositories() { jq -r '.extensions[].repository' "$set_file" | sort -u; }

by_branch() {
  local branch=$1 repository sha count
  if [[ ! "$branch" =~ ^[A-Za-z0-9._/-]+$ ]]; then
    echo "::error::The pull request branch name has characters this step does not accept."
    exit 1
  fi
  while read -r repository; do
    if sha=$(gh api "repos/$repository/branches/$branch" --jq .commit.sha 2>"$error"); then
      printf '%s=%s\n' "$repository" "$sha" >>"$out"
    else
      unanswered "branch $branch"
    fi
  done < <(repositories)
  count=$(grep -c . "$out" || true)
  echo "found=$([ "$count" -gt 0 ] && echo true || echo false)" >>"$GITHUB_OUTPUT"
  echo "coordinated extension branches named $branch: $count"
}

by_commit() {
  local commit=$1 repository base
  if [[ ! "$commit" =~ ^[0-9a-f]{40}$ ]]; then
    echo "::error::The extension commit is not a full SHA."
    exit 1
  fi
  # GitHub lists a commit's open and merged pull requests, a squashed one's
  # included (docs.github.com, list pull requests associated with a commit).
  local headed="[.[] | select(.head.sha == \"$commit\")] | sort_by(.state != \"open\", .merged_at == null)"
  while read -r repository; do
    if base=$(gh api "repos/$repository/commits/$commit/pulls" --jq "$headed | .[0].base.sha // empty" 2>"$error"); then
      [ -n "$base" ] || continue
      printf '%s=%s\n' "$repository" "$commit" >>"$out"
      printf 'base=%s\nname=%s\n' "$base" "${repository#*/}" >>"$GITHUB_OUTPUT"
    else
      unanswered "commit $commit"
    fi
  done < <(repositories)
  if [ "$(grep -c . "$out" || true)" -ne 1 ]; then
    echo "::error title=No extension pull request::No one extension repository has a pull request whose head is $commit."
    exit 1
  fi
  echo "found=true" >>"$GITHUB_OUTPUT"
}

case "${2:-}" in
  --commit) [ "$#" -eq 4 ] || usage; set_file=$1 out=$4; : >"$out"; by_commit "$3" ;;
  *) [ "$#" -eq 3 ] || usage; set_file=$1 out=$3; : >"$out"; by_branch "$2" ;;
esac
