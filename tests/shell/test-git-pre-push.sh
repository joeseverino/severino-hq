#!/usr/bin/env bash
# Prove git:pre-push lets a host branch go only when every extension checkout
# with work off main carries its name and is pushed, and refuses a push it
# cannot judge. Without an extension set, mise skips it (its skip_if).
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
task="$repo_dir/mise-tasks/git/pre-push"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=example GIT_AUTHOR_EMAIL=dev@example.com
export GIT_COMMITTER_NAME=example GIT_COMMITTER_EMAIL=dev@example.com
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

commit() { git -C "$1" -c commit.gpgsign=false commit --no-verify -q --allow-empty -m "$2"; }
sha=$(printf 'a%.0s' $(seq 40))
zero=$(printf '0%.0s' $(seq 40))

# An extension checkout with its source under src/, cloned from a remote
# whose main it starts at.
git init -q --bare -b main "$work/origin.git"
git clone -q "$work/origin.git" "$work/example-alpha" 2>/dev/null
ext=$work/example-alpha
mkdir -p "$ext/src/example_alpha"
commit "$ext" base
git -C "$ext" push -q origin main

# pre-push PLUGINS PYTHONPATH [REF]: the task as the hook runs it, with the
# host branch REF (refs/heads/feature by default) on stdin.
pre_push() {
  printf '%s %s refs/heads/x %s\n' "${3:-refs/heads/feature}" "$sha" "$zero" |
    HQ_LOCAL_PLUGINS=$1 HQ_LOCAL_PYTHONPATH=$2 "$task" >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }
passes() { pre_push "$@" || fail "refused: $*"; }
refuses() { if pre_push "$@"; then fail "passed: $*"; fi; }

set_=example_alpha.plugin:plugin
missing=$work/example-gone/src

passes "$set_" "$ext/src"
refuses "$set_" ""
refuses "$set_" "$ext/src:$missing"
refuses "$set_" "$work"

# Work off main on another branch name: Compose would not find it.
git -C "$ext" switch -q -c other
commit "$ext" work
refuses "$set_" "$ext/src"

# The same name, not yet pushed, then pushed.
git -C "$ext" branch -q -m other feature
refuses "$set_" "$ext/src"
git -C "$ext" push -q -u origin feature
passes "$set_" "$ext/src"

# A commit since the push is not on GitHub as it is here.
commit "$ext" more
refuses "$set_" "$ext/src"

# Neither a tag nor a deletion asks anything of the extensions.
passes "$set_" "$missing" refs/tags/v1
printf 'refs/heads/feature %s refs/heads/feature %s\n' "$zero" "$sha" |
  HQ_LOCAL_PLUGINS=$set_ HQ_LOCAL_PYTHONPATH=$missing "$task" >"$work/out" 2>&1 ||
  fail "a deletion was refused"

echo "git:pre-push holds a host branch to its extension branches"
