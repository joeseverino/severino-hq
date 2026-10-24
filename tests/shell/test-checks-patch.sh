#!/usr/bin/env bash
# Prove checks:patch reads the whole tree: a trailing space, a blank last line
# or a conflict marker fails it, committed long ago or left in the working tree.
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

repo=$work/repo
git init -q -b main "$repo"
printf 'clean\n' >"$repo/base.txt"
commit "$repo" base
printf 'more\n' >"$repo/more.txt"
commit "$repo" more

passes "$repo" "a clean tree was refused"

# Committed two commits back, where no diff from the last commit's parent looks.
printf 'clean\nold \n' >"$repo/base.txt"
commit "$repo" "trailing space"
printf 'later\n' >"$repo/later.txt"
commit "$repo" later
refuses "$repo" "a trailing space committed before the last commit passed"
printf 'clean\n' >"$repo/base.txt"
commit "$repo" fixed
passes "$repo" "a fixed tree was refused"

printf 'trailing \n' >"$repo/more.txt"
refuses "$repo" "a trailing space in the working tree passed"
git -C "$repo" checkout -q -- more.txt

printf 'more\n\n' >"$repo/more.txt"
refuses "$repo" "a blank last line in the working tree passed"
git -C "$repo" checkout -q -- more.txt

printf 'a\n=======\nb\n' >"$repo/marked.txt"
commit "$repo" marked
refuses "$repo" "a committed conflict marker passed"

echo "checks:patch reads every tracked file"
