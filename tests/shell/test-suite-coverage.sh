#!/bin/sh
# Prove test:suite has coverage judge its own floor (pyproject.toml) on the one
# report it reads the total from, and says that same total in the step summary.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
task=mise-tasks/test/suite
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

fixture="${work}/checkout"
mkdir -p "${fixture}/mise-tasks/test" "${fixture}/tools/lib" "${work}/bin"
cp "${repo}/${task}" "${fixture}/${task}"
cp "${repo}/tools/lib/heavy.sh" "${repo}/tools/lib/heavy-turn.sh" "${fixture}/tools/lib/"

# uv: the suite passes, and coverage's total is TEST_TOTAL. Every coverage
# report is logged and ends as TEST_VERDICT says its configured floor did.
cat >"${work}/bin/uv" <<'STUB'
#!/bin/sh
shift
args="$*"
case "${args}" in
    "coverage run"*) echo "Ran 3 tests in 0.100s" ;;
    "coverage report"*)
        echo "${args}" >>"${TEST_REPORTS}"
        echo "${TEST_TOTAL}"
        [ "${TEST_VERDICT}" = 0 ] || echo "Coverage failure: total of ${TEST_TOTAL} is less than fail-under=90"
        exit "${TEST_VERDICT}" ;;
esac
STUB
chmod +x "${work}/bin/uv"

failures=0
fail() { echo "FAIL $*" >&2; failures=$((failures + 1)); }

judge() { # judge <total> <verdict>; prints the task's exit status
    : >"${work}/reports"
    : >"${work}/summary"
    status=0
    # CI: one runner, one job, so the task takes no machine-wide turn. Run as
    # mise runs a file task: in its config root, named by MISE_CONFIG_ROOT.
    (cd "${fixture}" && CI=1 PATH="${work}/bin:${PATH}" TEST_TOTAL="$1" TEST_VERDICT="$2" \
        TEST_REPORTS="${work}/reports" GITHUB_STEP_SUMMARY="${work}/summary" MISE_CONFIG_ROOT="${fixture}" \
        "./${task}") >"${work}/out" 2>&1 || status=$?
    echo "${status}"
}

for case in 91.50:0 89.99:2; do
    total="${case%%:*}"
    want="${case#*:}"
    got="$(judge "${total}" "${want}")"
    [ "${got}" = "${want}" ] || fail "coverage ${total}%: exit ${got}, not ${want}
$(cat "${work}/out")"
    [ "$(grep -c '' "${work}/reports"):$(grep -c -e '--fail-under' "${work}/reports")" = 1:0 ] ||
        fail "coverage ${total}%: its configured floor is not judged on the one report the total is read from
$(cat "${work}/reports")"
    if [ "${want}" = 0 ]; then
        grep -qF "coverage ${total}%" "${work}/summary" ||
            fail "coverage ${total}%: the summary does not say the total judged"
    else
        grep -qF "Coverage failure: total of ${total} is less than fail-under=90" "${work}/out" ||
            fail "coverage ${total}%: coverage's verdict is not shown"
    fi
done

if [ "${failures}" -ne 0 ]; then
    echo "test:suite coverage contracts failed (${failures})." >&2
    exit 1
fi
echo "test:suite coverage contracts hold."
