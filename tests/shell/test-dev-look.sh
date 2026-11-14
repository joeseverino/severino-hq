#!/bin/sh
# Prove dev:look gives each run a database of its own, copied from the kept
# one, and that only a run that ended on its own, its database whole, hands it
# back as the kept one: looks started together share none.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
task=mise-tasks/dev/look
work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

fixture="${work}/checkout"
mkdir -p "${fixture}/$(dirname "${task}")" "${work}/bin"
cp "${repo}/${task}" "${fixture}/${task}"
# shellcheck source=tests/shell/task-libraries.sh
. "${repo}/tests/shell/task-libraries.sh"
copy_libraries "${repo}" "${fixture}" "${task}"

# uv stands for the look: it says which database it was handed and what that
# held, writes TEST_WRITES there, and ends as TEST_UV says: an exit status,
# `killed` by a signal, or `open` with pages still in the database's log.
cat >"${work}/bin/uv" <<'STUB'
#!/bin/sh
case "$*" in *" manage.py test "*) ;; *) exit 0 ;; esac
database="${SEVERINO_TEST_DATABASE_PATH}"
{ echo "${database}"; cat "${database}" 2>/dev/null || echo new; } >"${TEST_SEEN}"
echo "${TEST_WRITES}" >"${database}"
case "${TEST_UV}" in
    killed) kill -KILL "$$" ;;
    open) : >"${database}-wal" ;;
    *) exit "${TEST_UV}" ;;
esac
STUB
chmod +x "${work}/bin/uv"

look() { # look <uv behavior> <what the run writes>; the task's exit status is its own
    (cd "${fixture}" && env PATH="${work}/bin:${PATH}" TEST_UV="$1" TEST_WRITES="$2" TEST_SEEN="${work}/seen" \
        MISE_CONFIG_ROOT="${fixture}" MISE_TASK_LOCKS=heavy HQ_MEASURED=1 \
        usage_pages=example usage_engine=webkit "./${task}") >/dev/null 2>&1 || true
}

fail() { echo "$*" >&2; exit 1; }
handed() { sed -n 1p "${work}/seen"; }
held() { sed -n 2p "${work}/seen"; }

kept="${fixture}/var/dev-look/test.sqlite3"

look 0 first
[ "$(held)" = new ] || fail "the first run was handed a database that held $(held)"
[ "$(handed)" != "${kept}" ] || fail "a run was handed the kept database itself, which a run beside it would share"
[ ! -e "$(handed)" ] || fail "the run's own database was left behind: $(handed)"
[ "$(cat "${kept}")" = first ] || fail "a run that ended did not hand its database back"
first="$(handed)"

# A failed look ended on its own all the same: its database is the kept one.
look 1 second
[ "$(held)" = first ] || fail "the second run did not start from the kept database: it held $(held)"
[ "$(handed)" != "${first}" ] || fail "two runs were handed one database"
[ "$(cat "${kept}")" = second ] || fail "a look that failed did not hand its database back"

# A run that was killed, or left pages in its database's log, hands nothing back.
for ended in killed open; do
    look "${ended}" lost
    [ "$(held)" = second ] || fail "after a ${ended} run, the next started from $(held)"
    [ "$(cat "${kept}")" = second ] || fail "a ${ended} run handed its database back"
    [ ! -e "$(handed)" ] || fail "a ${ended} run's database was left behind"
done

echo "dev:look: each run has its own database, and only a clean end hands it back"
