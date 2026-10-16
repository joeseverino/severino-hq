#!/bin/sh
# The structural bar from AGENTS.md, as a gate: index this checkout in a code
# knowledge graph and fail on a near-duplicate function pair outside tests that
# the baseline does not list. Tests answer "does this behave?"; this answers
# "is it still one system?". A file's size is a test's (FileSizeTests).
#
#   scripts/structural-bar.sh            # check
#   scripts/structural-bar.sh --record   # rewrite the baseline from this tree
#
# Needs codebase-memory-mcp (https://github.com/DeusData/codebase-memory-mcp).
# Exit 3 when it is missing, which fails the gate (`mise run checks:structural`).
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
baseline="${repo}/scripts/structural-baseline.txt"
cbm="${CODEBASE_MEMORY_BIN:-codebase-memory-mcp}"
project="structural-bar-$(printf '%s' "${repo}" | cksum | cut -d' ' -f1)"

command -v "${cbm}" >/dev/null 2>&1 || {
    echo "codebase-memory-mcp is not installed; the structural bar did not run." >&2
    exit 3
}
# The answer's shape is the pinned release's; another release would be misread.
want="$(sed -n '/^\[tools."github:DeusData\/codebase-memory-mcp"\]$/{n;s/^version = "\(.*\)"$/\1/p;}' "${repo}/mise.toml")"
have="$("${cbm}" --version 2>/dev/null | awk '{print $2}')"
if [ "${have}" != "${want}" ]; then
    echo "codebase-memory-mcp ${have:-unknown} is not the pinned ${want}; run it through mise (mise run checks:structural)." >&2
    exit 1
fi

cli() { # cli <tool> <json args>
    printf '%s' "$2" | "${cbm}" cli --json "$1" 2>"${errors}"
}

errors="$(mktemp)"
# The tool says why it could not index; without that this fails silently.
cli index_repository "{\"repo_path\":\"${repo}\",\"mode\":\"full\",\"name\":\"${project}\"}" >/dev/null || {
    cat "${errors}" >&2
    rm -f "${errors}"
    exit 1
}

current="$(mktemp)"
graph="$(mktemp)"
inspector="$(mktemp)"
trap 'rm -f "${current}" "${graph}" "${inspector}" "${errors}"' EXIT
# Syntax inspection uses the Go standard library; provider code is never loaded.
go build -o "${inspector}" "${repo}/scripts/structural_go.go"
STRUCTURAL_INSPECTOR="${inspector}" python3 "${repo}/scripts/test_structural_classify.py"

# Every page of the answer, one JSON answer per line: the graph pages a large
# result, and the classifier refuses one that does not add up to its total.
query='MATCH (a)-[r:SIMILAR_TO]->(b) RETURN a.file_path, b.file_path, a.name, b.name'
cursor=""
: >"${graph}"
while :; do
    page="$(cli query_graph "$(jq -nc --arg p "${project}" --arg q "${query}" --arg c "${cursor}" \
        '{project: $p, query: $q, format: "json", max_rows: 99998} + (if $c == "" then {} else {cursor: $c} end)')")"
    printf '%s\n' "${page}" >>"${graph}"
    cursor="$(printf '%s' "${page}" | jq -r '.content[0].text | fromjson | if .has_more then .next_cursor else "" end')"
    [ -n "${cursor}" ] || break
done
python3 "${repo}/scripts/structural_classify.py" "${repo}" "${inspector}" <"${graph}" >"${current}"

if [ "${1:-}" = "--record" ]; then
    {
        echo "# Structural baseline for scripts/structural-bar.sh: each near-duplicate"
        echo "# pair outside tests. Pairs only leave. Rewrite with --record."
        cat "${current}"
    } >"${baseline}"
    echo "structural baseline recorded"
    exit 0
fi

failed=0
while read -r kind first second; do
    [ "${kind}" = similar ] || continue
    grep -qxF "similar ${first} ${second}" "${baseline}" || {
        echo "new near-duplicate outside tests: ${first} ${second}" >&2
        failed=1
    }
done <"${current}"

[ "${failed}" -eq 0 ] && echo "structural bar holds"
exit "${failed}"
