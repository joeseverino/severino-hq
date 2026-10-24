#!/usr/bin/env bash
# Prove checks:patch reads the change itself: a trailing space or a conflict
# marker committed on a branch fails it, as does one left in the working tree,
# a pull request's merge commit, checked out alone, is held to its base, and a
# conflict marker anywhere in the tree fails it.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
patch="$repo_dir/mise-tasks/checks/patch"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=example GIT_AUTHOR_EMAIL=dev@example.com
export GIT_COMMITTER_NAME=example GIT_COMMITTER_EMAIL=dev@example.com
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

commit() { git -C "$1" add -A && git -C "$1" -c commit.gpgsign=false commit --no-verify -q -m "$2"; }
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }
passes() { (cd "$1" && "$patch") >"$work/out" 2>&1 || fail "$2"; }
refuses() { if (cd "$1" && "$patch") >"$work/out" 2>&1; then fail "$2"; fi; }

# A remote whose main already holds a trailing space: what is on main is not
# the change's, so no branch is failed for it.
git init -q -b main "$work/origin"
printf 'clean\nold \n' >"$work/origin/base.txt"
commit "$work/origin" base
printf 'more\n' >"$work/origin/more.txt"
commit "$work/origin" more
git clone -q "$work/origin" "$work/repo"
repo=$work/repo
git -C "$repo" switch -q -c change

passes "$repo" "a branch with no change was refused"

printf 'fine\n' >"$repo/new.txt"
commit "$repo" clean
passes "$repo" "a clean change was refused"

printf 'trailing \n' >"$repo/new.txt"
commit "$repo" "trailing space"
refuses "$repo" "a committed trailing space passed"
git -C "$repo" reset -q --hard HEAD~1

printf '<<<<<<< ours\na\n=======\nb\n>>>>>>> theirs\n' >"$repo/new.txt"
commit "$repo" "conflict marker"
refuses "$repo" "a committed conflict marker passed"
git -C "$repo" reset -q --hard HEAD~1

printf 'trailing \n' >"$repo/new.txt"
refuses "$repo" "a trailing space in the working tree passed"
git -C "$repo" checkout -q -- new.txt

# A pull request's merge commit, as CI checks it out: no origin/main, and
# main's tip as the first parent.
printf 'trailing \n' >"$repo/new.txt"
commit "$repo" "trailing space"
git -C "$repo" switch -q --detach origin/main
git -C "$repo" -c commit.gpgsign=false merge --no-verify --no-ff -q -m merge change
git -C "$repo" branch -q -D change
git -C "$repo" update-ref -d refs/remotes/origin/main
git -C "$repo" update-ref -d refs/remotes/origin/HEAD 2>/dev/null || true
refuses "$repo" "a merge commit's trailing space passed"

# A conflict marker is refused wherever it is, even one main already holds.
printf 'a\n=======\nb\n' >"$work/origin/marked.txt"
commit "$work/origin" marked
printf 'more\n' >>"$work/origin/more.txt"
commit "$work/origin" after
git clone -q "$work/origin" "$work/marked"
git -C "$work/marked" switch -q -c change
refuses "$work/marked" "a conflict marker already on main passed"

echo "checks:patch reads the change from its base"
