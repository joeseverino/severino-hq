# shellcheck shell=sh
# The test database a look keeps between runs, taken in one line before the
# look and handed back after it:
#   . ./tools/lib/look-database.sh
#   ... manage.py test ... ${keep} || looked=$?
#   hand_back_look_database "${looked}"
# The database outlives the run (Django's --keepdb), so the next run migrates
# only what is new. Each run works on a copy of its own and hands it back as
# the kept one when it ends, by a rename, so looks started together never
# share a database, and a run that did not come back, which may have left its
# estate committed, hands nothing back. `kept_set` names a set of its own (a
# composed look's holds the extensions' tables).
kept="${MISE_CONFIG_ROOT}/var/dev-look${kept_set:-}"
mkdir -p "${kept}"
# What a run that was killed left behind.
find "${kept}" -maxdepth 1 -name 'run.*' -mmin +120 -exec rm -rf {} +
own="$(mktemp -d "${kept}/run.XXXXXX")"
trap 'rm -rf "${own}"' EXIT
[ ! -f "${kept}/test.sqlite3" ] || cp "${kept}/test.sqlite3" "${own}/test.sqlite3"
SEVERINO_TEST_DATABASE_PATH="${own}/test.sqlite3"
export SEVERINO_TEST_DATABASE_PATH
# shellcheck disable=SC2034 # read by the task that sources this
keep=--keepdb

# Handed back by a run that ended on its own, and whole: a database closed
# with pages still in its log is left out.
hand_back_look_database() {
    if [ "$1" -lt 128 ] && [ -f "${own}/test.sqlite3" ] && [ ! -e "${own}/test.sqlite3-wal" ]; then
        mv -f "${own}/test.sqlite3" "${kept}/test.sqlite3"
    fi
}
