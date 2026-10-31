#!/bin/sh
# Prove test:composed removes its copy of the checkout however the suite ends,
# fails when the suite fails, ends on the gate's verdict kept in var/gate, and
# touches no other run's copy.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
task=mise-tasks/test/composed
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# A checkout with what the task reads: the image admits manage.py, not notes/,
# and admits built/ only for a stage that is not /app's.
fixture="${work}/checkout"
mkdir -p "${fixture}/$(dirname "${task}")" "${fixture}/tools/lib" "${fixture}/tools/gate" "${fixture}/notes" "${fixture}/built" "${work}/bin" "${work}/tmp"
cp "${repo}/${task}" "${fixture}/${task}"
cp "${repo}/tools/lib/heavy.sh" "${repo}/tools/lib/heavy-turn.sh" "${fixture}/tools/lib/"
cp "${repo}/tools/lib/export-tree.sh" "${repo}/tools/lib/source-url.sh" "${repo}/tools/lib/extension-set.sh" "${fixture}/tools/lib/"
cp "${repo}/tools/__init__.py" "${fixture}/tools/"
cp "${repo}/tools/gate/__init__.py" "${repo}/tools/gate/verdict.py" "${fixture}/tools/gate/"
printf '*\n!built/\n!manage.py\n' >"${fixture}/Dockerfile.dockerignore"
printf 'FROM scratch\nCOPY --exclude=built . /app\n' >"${fixture}/Dockerfile"
: >"${fixture}/manage.py"
: >"${fixture}/built/main.go"
: >"${fixture}/notes/private.md"
git -C "${fixture}" init -q
git -C "${fixture}" remote add origin https://example.com/hq.git
git -C "${fixture}" add -A
git -C "${fixture}" -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false commit -q --no-verify -m fixture

# uv runs the gate's verdict as it is, from where it was asked to run; the
# suite it runs records where it ran, then ends as TEST_UV says: an exit
# status, or a signal to the task while the suite is still running.
cat >"${work}/uv-run" <<'STUB'
[ "$1" = run ] && shift
while :; do
    case "$1" in
        --directory) cd "$2"; shift 2 ;;
        --*) shift ;;
        *) break ;;
    esac
done
case "$*" in "python -m tools.gate.verdict "*) shift 3; exec python3 -m tools.gate.verdict "$@" ;; esac
STUB
{ echo '#!/bin/sh'; cat "${work}/uv-run"; } >"${work}/bin/uv"
cat >>"${work}/bin/uv" <<'STUB'
pwd >"${TEST_SEEN}"
case "${TEST_UV}" in
    term) kill -TERM "${PPID}" ;;
    *) exit "${TEST_UV}" ;;
esac
STUB
chmod +x "${work}/bin/uv"

# Another run's copy, which this one must leave alone.
other="${work}/tmp/severino-test-tree-1-other"
mkdir "${other}"

run() { # run <uv behavior> [NAME=value...]; prints the task's exit status
    behavior="$1"
    shift
    status=0
    # Its own heavy turn, so a heavy command elsewhere on the machine cannot
    # refuse it. Run as mise runs a file task: in its config root, named by
    # MISE_CONFIG_ROOT, with the bound its env declares. No extension set unless
    # a case names one.
    (cd "${fixture}" && env PATH="${work}/bin:${PATH}" TMPDIR="${work}/tmp" XDG_CACHE_HOME="${work}/cache" \
        TEST_UV="${behavior}" TEST_SEEN="${work}/seen" MISE_CONFIG_ROOT="${fixture}" \
        SEVERINO_HEAVY_BOUND=1m HQ_LOCAL_PLUGINS= REQUIRE_COMPOSED= HQ_EXTENSIONS_SAID= "$@" "./${task}") >/dev/null 2>&1 || status=$?
    echo "${status}"
}

fail() { echo "$*" >&2; exit 1; }

for case in 0:0 3:1 term:143; do
    behavior="${case%%:*}"
    want="${case#*:}"
    rm -f "${work}/seen"
    got="$(run "${behavior}")"
    [ "${got}" = "${want}" ] || fail "uv ${behavior}: the task exited ${got}, not ${want}"
    seen="$(cat "${work}/seen")"
    case "${seen}" in "${work}/tmp/severino-test-tree-"*) ;; *) fail "the suite ran in ${seen}, not a copy" ;; esac
    [ ! -e "${seen}" ] || fail "uv ${behavior}: the copy ${seen} was left behind"
    [ -d "${other}" ] || fail "uv ${behavior}: another run's copy was removed"
done

# The run's verdict is its closing line, kept whole where the line above names.
rm -f "${fixture}/var/gate/test-composed.txt"
(cd "${fixture}" && env PATH="${work}/bin:${PATH}" TMPDIR="${work}/tmp" XDG_CACHE_HOME="${work}/cache" \
    TEST_UV=3 TEST_SEEN="${work}/seen" MISE_CONFIG_ROOT="${fixture}" SEVERINO_HEAVY_BOUND=1m HQ_LOCAL_PLUGINS= REQUIRE_COMPOSED= \
    "./${task}") >"${work}/out" 2>&1 || true
kept="$(cd "${fixture}" && pwd -P)/var/gate/test-composed.txt"
[ -f "${kept}" ] || fail "the run kept no verdict in var/gate"
grep -qF "verdict kept in ${kept}" "${work}/out" || fail "the output does not name the file that keeps the verdict"
case "$(grep "^\[test:composed\]" "${work}/out" | tail -n 1)" in "[test:composed] FAILED"*) ;; *) fail "the run does not end on its verdict" ;; esac

# Required without an extension set, it refuses before the suite runs; with
# one, it runs.
rm -f "${work}/seen"
[ "$(run 0 REQUIRE_COMPOSED=1)" = 1 ] || fail "required without an extension set, the task did not refuse"
[ ! -e "${work}/seen" ] || fail "required without an extension set, the suite ran anyway"
[ "$(run 0 REQUIRE_COMPOSED=1 HQ_LOCAL_PLUGINS=example_alpha.plugin:plugin)" = 0 ] ||
    fail "required with an extension set, the task did not run"

# A composed run's first line names the tree each package is imported from,
# and the suite imports from there.
{ echo '#!/bin/sh'; cat "${work}/uv-run"; } >"${work}/bin/uv"
cat >>"${work}/bin/uv" <<'STUB'
echo "${SEVERINO_HQ_PLUGINS}|${PYTHONPATH}" >"${TEST_SEEN}.set"
STUB
sources="${work}/alpha/src:${work}/beta-worktree/src"
(cd "${fixture}" && env PATH="${work}/bin:${PATH}" TMPDIR="${work}/tmp" XDG_CACHE_HOME="${work}/cache" \
    TEST_UV=0 TEST_SEEN="${work}/seen" MISE_CONFIG_ROOT="${fixture}" SEVERINO_HEAVY_BOUND=1m REQUIRE_COMPOSED= HQ_EXTENSIONS_SAID= \
    HQ_LOCAL_PLUGINS=example_alpha.plugin:plugin,example_beta.plugin:plugin HQ_LOCAL_PYTHONPATH="${sources}" \
    "./${task}") >"${work}/out" 2>&1 || fail "a composed run failed"
first="$(head -n 1 "${work}/out")"
case "${first}" in *"${work}/alpha/src, ${work}/beta-worktree/src"*) ;; *) fail "the first line does not name each package's tree: ${first}" ;; esac
[ "$(grep -c "composed from" "${work}/out")" = 1 ] || fail "the trees are named more than once"
[ "$(cat "${work}/seen.set")" = "example_alpha.plugin:plugin,example_beta.plugin:plugin|${sources}" ] ||
    fail "the suite did not import the set named: $(cat "${work}/seen.set")"

# The copy holds only what the image admits.
rm -f "${work}/seen"
{ echo '#!/bin/sh'; cat "${work}/uv-run"; } >"${work}/bin/uv"
cat >>"${work}/bin/uv" <<'STUB'
[ -f manage.py ] && [ ! -e notes ] && [ ! -e built ] && [ ! -e .git ]
STUB
[ "$(run 0)" = 0 ] || fail "the copy holds more than the image admits"

echo "test:composed tests passed"
