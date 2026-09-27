#!/bin/sh
# The structural bar from AGENTS.md, as a gate: index this checkout in a code
# knowledge graph and fail on a near-duplicate function pair outside tests that
# the baseline does not list, or a largest source file that grew past its
# baselined length. Tests answer "does this behave?"; this answers "is it still
# one system?".
#
#   scripts/structural-bar.sh            # check
#   scripts/structural-bar.sh --record   # rewrite the baseline from this tree
#
# Needs codebase-memory-mcp (https://github.com/DeusData/codebase-memory-mcp).
# Exit 3 when it is missing, so ci-local reports the gate as not run.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
baseline="${repo}/scripts/structural-baseline.txt"
cbm="${CODEBASE_MEMORY_BIN:-codebase-memory-mcp}"
project="structural-bar-$(printf '%s' "${repo}" | cksum | cut -d' ' -f1)"

command -v "${cbm}" >/dev/null 2>&1 || {
    echo "codebase-memory-mcp is not installed; the structural bar did not run." >&2
    exit 3
}

cli() { # cli <tool> <json args>
    printf '%s' "$2" | "${cbm}" cli --json "$1" 2>/dev/null
}

cli index_repository "{\"repo_path\":\"${repo}\",\"mode\":\"full\",\"name\":\"${project}\"}" >/dev/null

current="$(mktemp)"
trap 'rm -f "${current}"' EXIT

cli query_graph "{\"project\":\"${project}\",\"query\":\"MATCH (a)-[r:SIMILAR_TO]->(b) RETURN a.file_path, a.name, b.file_path, b.name\"}" |
    python3 -c '
import json, re, sys
text = json.load(sys.stdin)["content"][0]["text"]
test = re.compile(r"(^|/)(test_[^/]*|tests|[^/]*_tests)\.py$|(^|/)tests/")
pairs = set()
for line in text.splitlines()[1:]:
    cells = line.split()
    if len(cells) != 4 or line.startswith("total"):
        continue
    a, b = (cells[0], cells[1]), (cells[2], cells[3])
    if test.search(a[0]) or test.search(b[0]):
        continue
    pairs.add("similar " + " <> ".join(sorted(f"{path}::{name}" for path, name in (a, b))))
print("\n".join(sorted(pairs)))
' >"${current}"

cd "${repo}"
git ls-files '*.py' | grep -Ev '(^|/)(test_[^/]*|tests|[^/]*_tests)\.py$|(^|/)tests/|/migrations/' |
    xargs wc -l | grep -v ' total$' | sort -n | tail -4 |
    awk '{printf "largest %s %s\n", $2, $1}' >>"${current}"

if [ "${1:-}" = "--record" ]; then
    {
        echo "# Structural baseline for scripts/structural-bar.sh. A near-duplicate pair"
        echo "# outside tests, and the four largest source files with their line counts."
        echo "# Pairs only leave; a file's count only shrinks. Rewrite with --record."
        cat "${current}"
    } >"${baseline}"
    echo "structural baseline recorded"
    exit 0
fi

failed=0
while read -r kind first second; do
    case "${kind}" in
        similar)
            line="similar ${first} ${second}"
            grep -qxF "${line}" "${baseline}" || {
                echo "new near-duplicate outside tests: ${first} ${second}" >&2
                failed=1
            } ;;
        largest)
            allowed="$(awk -v f="${first}" '$1 == "largest" && $2 == f {print $3}' "${baseline}")"
            if [ -z "${allowed}" ]; then
                echo "${first} (${second} lines) is now among the largest files; split it or record it" >&2
                failed=1
            elif [ "${second}" -gt "${allowed}" ]; then
                echo "${first} grew from ${allowed} to ${second} lines; the largest files only shrink" >&2
                failed=1
            fi ;;
    esac
done <"${current}"

[ "${failed}" -eq 0 ] && echo "structural bar holds"
exit "${failed}"
