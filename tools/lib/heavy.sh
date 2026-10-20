#!/bin/sh
# Runs a heavy command (a suite, a browser gate, a demo build) only while no
# other one runs on this machine, so two never share its memory:
#   tools/lib/heavy.sh <command...>
#
# A TOML task takes it as its shell (the heavy template in mise.toml); a file
# task, which cannot set one, sources tools/lib/heavy-turn.sh.
#
# The turn is an exclusive flock(2) lock on one file every checkout and
# worktree shares, taken without waiting: while another command holds it, this
# names the holder and exits 75 (EX_TEMPFAIL). The lock is taken on a
# descriptor the command inherits, so it lasts as long as the command and what
# it started, and a killed holder frees it. A heavy command started inside a
# turn runs at once: the holder exports its pid, and a process descended from it
# is inside that turn. A CI runner runs one job, and a system with neither
# lockf(1) nor flock(1) has no lock to take: both run the command directly.
set -eu

[ "$#" -gt 0 ] || { echo "usage: tools/lib/heavy.sh <command...>" >&2; exit 64; }

inside_turn() {
    holder="${SEVERINO_HEAVY_HOLDER:-}"
    [ -n "${holder}" ] || return 1
    pid=$$
    while [ "${pid}" -gt 1 ]; do
        [ "${pid}" = "${holder}" ] && return 0
        pid="$(ps -o ppid= -p "${pid}" 2>/dev/null | tr -d ' ')"
        [ -n "${pid}" ] || return 1
    done
    return 1
}

if [ -n "${CI:-}" ] || inside_turn; then
    exec "$@"
fi
if command -v lockf >/dev/null 2>&1; then
    take() { lockf -s -t 0 9; }
elif command -v flock >/dev/null 2>&1; then
    take() { flock -n -E 75 9; }
else
    exec "$@"
fi

lock="${XDG_CACHE_HOME:-${HOME}/.cache}/severino/heavy.lock"
mkdir -p "${lock%/*}"

# The holder writes its pid into the file; if that one is gone while the lock
# is still held, a process it started holds it, and lsof finds which.
describe_holder() {
    pid="$(cat "${lock}" 2>/dev/null || true)"
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        pid="$(lsof -t -- "${lock}" 2>/dev/null | grep -vx "$$" | head -n 1 || true)"
    fi
    if [ -n "${pid}" ] && ps -p "${pid}" >/dev/null 2>&1; then
        printf 'pid %s, running %s: %s' "${pid}" "$(ps -o etime= -p "${pid}" | tr -d ' ')" "$(ps -o command= -p "${pid}")"
    else
        printf 'a process lsof cannot name (%s)' "${lock}"
    fi
}

exec 9>>"${lock}"
status=0
take || status=$?
if [ "${status}" -eq 75 ]; then
    echo "heavy: another heavy command holds the turn, $(describe_holder). Wait for it, or stop it if it is stuck." >&2
    exit 75
fi
[ "${status}" -eq 0 ] || exit "${status}"

echo "$$" >"${lock}"
SEVERINO_HEAVY_HOLDER=$$
export SEVERINO_HEAVY_HOLDER
exec "$@"
