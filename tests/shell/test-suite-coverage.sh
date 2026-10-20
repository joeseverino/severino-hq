#!/bin/sh
# Prove tests:suite judges coverage against its floor as decimals, reads the
# total once, and says that same total in the step summary.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
task=mise-tasks/tests/suite
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

fixture="${work}/checkout"
mkdir -p "${fixture}/mise-tasks/tests" "${fixture}/tools/lib" "${work}/bin"
cp "${repo}/${task}" "${fixture}/${task}"
cp "${repo}/tools/lib/heavy.sh" "${repo}/tools/lib/heavy-turn.sh" "${fixture}/tools/lib/"

# uv: the suite passes, and coverage's total is TEST_TOTAL. Every coverage
# report is logged, and one asked to judge a floor judges it as coverage does.
cat >"${work}/bin/uv" <<'STUB'
#!/bin/sh
shift
args="$*"
case "${args}" in
    "coverage run"*) echo "Ran 3 tests in 0.100s" ;;
    "coverage report"*)
        echo "${args}" >>"${TEST_REPORTS}"
        case "${args}" in
            *--fail-under=*)
                floor="${args##*--fail-under=}"
                awk -v total="${TEST_TOTAL}" -v floor="${floor}" 'BEGIN { exit !(total + 0 >= floor + 0) }' || exit 2 ;;
            *) echo "${TEST_TOTAL}" ;;
        esac ;;
esac
STUB
chmod +x "${work}/bin/uv"

failures=0
fail() { echo "FAIL $*" >&2; failures=$((failures + 1)); }

judge() { # judge <total> <floor>; prints the task's exit status
    : >"${work}/reports"
    : >"${work}/summary"
    status=0
    # CI: one runner, one job, so the task takes no machine-wide turn. Run as
    # mise runs a file task: in its config root, named by MISE_CONFIG_ROOT.
    (cd "${fixture}" && CI=1 PATH="${work}/bin:${PATH}" TEST_TOTAL="$1" COVERAGE_FLOOR="$2" \
        TEST_REPORTS="${work}/reports" GITHUB_STEP_SUMMARY="${work}/summary" MISE_CONFIG_ROOT="${fixture}" \
        "./${task}") >"${work}/out" 2>&1 || status=$?
    echo "${status}"
}

for case in 91.50:91.49:0 91.48:91.49:2 10.00:9:0 100.00:99.5:0 9.99:10:2; do
    total="${case%%:*}"
    rest="${case#*:}"
    floor="${rest%%:*}"
    want="${rest#*:}"
    got="$(judge "${total}" "${floor}")"
    [ "${got}" = "${want}" ] || fail "coverage ${total}% against ${floor}%: exit ${got}, not ${want}
$(cat "${work}/out")"
    [ "$(wc -l <"${work}/reports" | tr -d ' ')" = 1 ] || fail "coverage ${total}%: the total was read more than once
$(cat "${work}/reports")"
    if [ "${want}" = 0 ]; then
        grep -qF "coverage ${total}% (floor ${floor}%)" "${work}/summary" ||
            fail "coverage ${total}%: the summary does not say the total judged"
    else
        grep -qF "coverage ${total}% is under the floor of ${floor}%" "${work}/out" ||
            fail "coverage ${total}%: the shortfall does not name its numbers"
    fi
done

if [ "${failures}" -ne 0 ]; then
    echo "tests:suite coverage contracts failed (${failures})." >&2
    exit 1
fi
echo "tests:suite coverage contracts hold."
