#!/bin/sh
# Prove suite:as-image removes its copy of the checkout however the suite ends,
# passes the suite's exit status on, and touches no other run's copy.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
task=mise-tasks/suite/as-image
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# A checkout with what the task reads: the image admits manage.py, not notes/.
fixture="${work}/checkout"
mkdir -p "${fixture}/$(dirname "${task}")" "${fixture}/tools/lib" "${fixture}/notes" "${work}/bin" "${work}/tmp"
cp "${repo}/${task}" "${fixture}/${task}"
cp "${repo}/tools/lib/heavy.sh" "${repo}/tools/lib/heavy-turn.sh" "${fixture}/tools/lib/"
cp "${repo}/tools/lib/export-tree.sh" "${repo}/tools/lib/source-url.sh" "${fixture}/tools/lib/"
printf '*\n!manage.py\n' >"${fixture}/Dockerfile.dockerignore"
: >"${fixture}/manage.py"
: >"${fixture}/notes/private.md"
git -C "${fixture}" init -q
git -C "${fixture}" remote add origin https://example.com/hq.git
git -C "${fixture}" add -A
git -C "${fixture}" -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false commit -q --no-verify -m fixture

# uv records where it ran, then ends as TEST_UV says: an exit status, or a
# signal to the task while the suite is still running.
cat >"${work}/bin/uv" <<'STUB'
#!/bin/sh
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

run() { # run <uv behaviour> [NAME=value...]; prints the task's exit status
    behaviour="$1"
    shift
    status=0
    # Its own heavy turn, so a heavy command elsewhere on the machine cannot
    # refuse it. Run as mise runs a file task: in its config root, named by
    # MISE_CONFIG_ROOT. No extension set unless a case names one.
    (cd "${fixture}" && env PATH="${work}/bin:${PATH}" TMPDIR="${work}/tmp" XDG_CACHE_HOME="${work}/cache" \
        TEST_UV="${behaviour}" TEST_SEEN="${work}/seen" MISE_CONFIG_ROOT="${fixture}" \
        HQ_LOCAL_PLUGINS= REQUIRE_COMPOSED= "$@" "./${task}") >/dev/null 2>&1 || status=$?
    echo "${status}"
}

fail() { echo "$*" >&2; exit 1; }

for case in 0:0 3:3 term:143; do
    behaviour="${case%%:*}"
    want="${case#*:}"
    rm -f "${work}/seen"
    got="$(run "${behaviour}")"
    [ "${got}" = "${want}" ] || fail "uv ${behaviour}: the task exited ${got}, not ${want}"
    seen="$(cat "${work}/seen")"
    case "${seen}" in "${work}/tmp/severino-test-tree-"*) ;; *) fail "the suite ran in ${seen}, not a copy" ;; esac
    [ ! -e "${seen}" ] || fail "uv ${behaviour}: the copy ${seen} was left behind"
    [ -d "${other}" ] || fail "uv ${behaviour}: another run's copy was removed"
done

# Required without an extension set, it refuses before the suite runs; with
# one, it runs.
rm -f "${work}/seen"
[ "$(run 0 REQUIRE_COMPOSED=1)" = 1 ] || fail "required without an extension set, the task did not refuse"
[ ! -e "${work}/seen" ] || fail "required without an extension set, the suite ran anyway"
[ "$(run 0 REQUIRE_COMPOSED=1 HQ_LOCAL_PLUGINS=example_alpha.plugin:plugin)" = 0 ] ||
    fail "required with an extension set, the task did not run"

# The copy holds only what the image admits.
rm -f "${work}/seen"
cat >"${work}/bin/uv" <<'STUB'
#!/bin/sh
[ -f manage.py ] && [ ! -e notes ] && [ ! -e .git ]
STUB
[ "$(run 0)" = 0 ] || fail "the copy holds more than the image admits"

echo "suite:as-image tests passed"
