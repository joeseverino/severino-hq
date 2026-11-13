#!/bin/sh
# Two branches that each lowered the page-class baseline, dropping different
# rows (adjacent ones included), rebase and merge-tree cleanly to the rows both
# kept once setup:git has defined the driver .gitattributes names, and conflict
# without it. Run under each /bin/sh, as the driver's command line is.
set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
shell="${SHELL_UNDER_TEST:-/bin/sh}"
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT HUP INT TERM

export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
export GIT_AUTHOR_NAME=example GIT_AUTHOR_EMAIL=dev@example.com
export GIT_COMMITTER_NAME=example GIT_COMMITTER_EMAIL=dev@example.com
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE

baseline=tools/lint/hand-rolled.txt
fail() {
    echo "FAIL $1" >&2
    [ ! -f "${work}/out" ] || cat "${work}/out" >&2
    exit 1
}
commit() { git -C "$1" add -A && git -C "$1" -c commit.gpgsign=false commit --no-verify -q -m "$2"; }
# Branch $2 from main in $1 with the baseline's lines $3 (a sed address list,
# `10d;11d`) deleted.
lowered() {
    git -C "$1" checkout -q -b "$2" main
    sed -e "$3" "${repo_dir}/${baseline}" >"$1/${baseline}"
    commit "$1" "$2"
}

repo="${work}/repo"
git init -q -b main "${repo}"
mkdir -p "${repo}/tools/lint"
cp "${repo_dir}/.gitattributes" "${repo}/"
cp "${repo_dir}/${baseline}" "${repo}/${baseline}"
commit "${repo}" base
rows=$(grep -cv '^#' "${repo}/${baseline}")
[ "${rows}" -ge 40 ] || fail "the baseline has too few rows to drop from: ${rows}"

# Each side drops a run of rows that touches the other's, and one far away.
lowered "${repo}" ours '10d;11d;30d'
lowered "${repo}" theirs '12d;13d;40d'
sed -e '10d;11d;12d;13d;30d;40d' "${repo_dir}/${baseline}" >"${work}/both-kept"
theirs=$(git -C "${repo}" rev-parse theirs)

# A text merge cannot place adjacent drops from two sides.
if git -C "${repo}" rebase -q ours >"${work}/out" 2>&1; then
    fail "the rebase merged without the driver, so this test cannot show the driver is what merges it"
fi
git -C "${repo}" rebase --abort

(cd "${repo}" && "${shell}" "${repo_dir}/mise-tasks/setup/git") >"${work}/out" 2>&1 ||
    fail "setup:git failed"

# A linked worktree reads the driver from the config it shares.
git -C "${repo}" checkout -q main
git -C "${repo}" worktree add -q "${work}/linked" theirs
git -C "${work}/linked" rebase -q ours >"${work}/out" 2>&1 ||
    fail "the rebase in a linked worktree conflicted with the driver set"
cmp -s "${work}/both-kept" "${work}/linked/${baseline}" ||
    fail "the rebase kept other rows than both sides kept"
[ -z "$(git -C "${work}/linked" status --porcelain)" ] ||
    fail "the rebase left files behind"

# merge-tree, how a merge is predicted without a checkout, merges it too.
tree=$(git -C "${repo}" merge-tree --write-tree --no-messages ours "${theirs}") ||
    fail "merge-tree conflicted with the driver set"
git -C "${repo}" cat-file blob "${tree}:${baseline}" >"${work}/predicted"
cmp -s "${work}/both-kept" "${work}/predicted" ||
    fail "merge-tree kept other rows than both sides kept"

# No line in common is still a clean merge, to an empty file.
git -C "${repo}" checkout -q -b emptied main
: >"${repo}/${baseline}"
commit "${repo}" emptied
lowered "${repo}" one-row '10d'
git -C "${repo}" rebase -q emptied >"${work}/out" 2>&1 ||
    fail "a merge with no line in common conflicted"
[ ! -s "${repo}/${baseline}" ] || fail "a merge with no line in common kept rows"

echo "intersect-lines merges two lowered baselines to the rows both kept"
