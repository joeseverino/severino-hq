#!/bin/sh
# Runs a heavy command (a suite, a browser gate, a demo build) only while no
# other one runs on this machine, so two never share its memory:
#   tools/lib/heavy.sh <command...>
#
# A TOML task takes it as its shell (the heavy template in mise.toml); a file
# task, which cannot set one, sources tools/lib/heavy-turn.sh.
#
# The turn is an exclusive flock(2) lock on one file every checkout and
# worktree shares. While another command holds it, this names the holder and
# waits for the turn, naming it again every SEVERINO_HEAVY_EVERY seconds (a
# minute), so a holder that has stalled is seen rather than read as this one
# hanging; past SEVERINO_HEAVY_WAIT seconds (two hours, the longest a heavy
# task may run) it names the holder once more and exits 75 (EX_TEMPFAIL). A
# hung holder is stopped by its task's own timeout, never by a waiter, since it
# may be another session's work. The lock lasts as long as this script, which
# waits for its command, so a command that ends or is stopped frees it. A heavy
# command started inside a turn runs at once: the holder exports its pid, and a
# process descended from it is inside that turn. A CI
# runner runs one job, and a system with neither lockf(1) nor flock(1) has no
# lock to take: both run the command directly.
#
# A turn prints the machine's load average as it starts and as it ends, so a
# gate's time can be read against what else the machine was doing.
set -eu

[ "$#" -gt 0 ] || { echo "usage: tools/lib/heavy.sh <command...>" >&2; exit 64; }

bound="${SEVERINO_HEAVY_WAIT:-7200}"
every="${SEVERINO_HEAVY_EVERY:-60}"
case "${bound}" in
    '' | *[!0-9]*) echo "heavy: SEVERINO_HEAVY_WAIT is a number of seconds, not ${bound}" >&2; exit 64 ;;
esac
case "${every}" in
    '' | 0 | *[!0-9]*) echo "heavy: SEVERINO_HEAVY_EVERY is a positive number of seconds, not ${every}" >&2; exit 64 ;;
esac

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

# bounded <command...>: runs the command as a process group of its own, and
# stops that whole group when this script is stopped. A mise task's timeout
# signals its process group, but a mise run from inside a task signals only
# the pid it started, so without the group a bound fired there would leave
# what the command started running. Perl's setpgrp makes the group: set -m is
# the shell's to refuse, and dash, Linux's /bin/sh, does without a terminal.
# Until perl has made it, the trap reaches perl by its pid. The command never
# holds the lock's descriptor: the turn lasts exactly as long as this script,
# which the timeout's SIGKILL ends whatever the command does. A command inside
# a turn runs as this pid (heavy-turn.sh knows its own run by it), in the
# turn's group, so the turn's bound reaches it.
bounded() {
    perl -e 'setpgrp; exec { $ARGV[0] } @ARGV; warn "heavy: $ARGV[0]: $!\n"; exit 127' -- "$@" 9>&- &
    job=$!
    trap 'kill -s TERM -- "-${job}" 2>/dev/null || kill -s TERM "${job}" 2>/dev/null || true' HUP INT TERM
    ended=0
    wait "${job}" || ended=$?
    # A trapped signal ends the wait early; the job is reaped once it exits.
    while kill -0 "${job}" 2>/dev/null; do
        ended=0
        wait "${job}" || ended=$?
    done
    return "${ended}"
}

if [ -n "${CI:-}" ] || inside_turn; then
    exec "$@"
fi
# take <seconds>: holds the lock on descriptor 9, or exits 75 once the seconds
# run out.
if command -v lockf >/dev/null 2>&1; then
    take() { lockf -s -t "$1" 9; }
elif command -v flock >/dev/null 2>&1; then
    take() { flock -w "$1" -E 75 9; }
else
    exec "$@"
fi

lock="${XDG_CACHE_HOME:-${HOME}/.cache}/severino/heavy.lock"
mkdir -p "${lock%/*}"

# Seconds as hours and minutes, or minutes and seconds: 7200 is 2h00m, 1203 is 20m03s.
span() {
    if [ "$1" -ge 3600 ]; then
        printf '%dh%02dm' "$(($1 / 3600))" "$(($1 % 3600 / 60))"
    else
        printf '%dm%02ds' "$(($1 / 60))" "$(($1 % 60))"
    fi
}

# Who holds the turn, in the words a reader acts on: the mise task and the
# checkout it runs in, how long it has held the turn, and its pid to stop it
# by. The holder writes its pid, the time it took the turn, its task and its
# checkout into the file; if that pid is gone while the lock is still held,
# the turn has passed to a holder that has not written it yet, and lsof finds
# which, named by its command.
describe_holder() {
    pid="" since="" task="" where=""
    read -r pid since task where <"${lock}" 2>/dev/null || true
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        pid="$(lsof -t -- "${lock}" 2>/dev/null | grep -vx "$$" | head -n 1 || true)"
        since="" task="" where=""
    fi
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        printf 'a process lsof cannot name (%s)' "${lock}"
        return
    fi
    case "${task}" in
        '' | -) printf '%s' "$(ps -o command= -p "${pid}")" ;;
        *) printf 'mise run %s' "${task}" ;;
    esac
    [ -z "${where}" ] || printf ' in %s' "${where}"
    case "${since}" in
        '' | *[!0-9]*) ;;
        *) printf ', for %s' "$(span "$(($(date +%s) - since))")" ;;
    esac
    printf ' (pid %s)' "${pid}"
}

exec 9>>"${lock}"
status=0
take 0 || status=$?
if [ "${status}" -eq 75 ]; then
    echo "heavy: waiting for the turn, held by $(describe_holder)" >&2
    waited=0
    while :; do
        step="${every}"
        [ "$((bound - waited))" -ge "${step}" ] || step="$((bound - waited))"
        status=0
        take "${step}" || status=$?
        [ "${status}" -eq 75 ] || break
        waited="$((waited + step))"
        if [ "${waited}" -ge "${bound}" ]; then
            echo "heavy: the turn is still held after $(span "${bound}"), by $(describe_holder). If it is stuck, kill that pid." >&2
            exit 75
        fi
        echo "heavy: still waiting after $(span "${waited}"), held by $(describe_holder)" >&2
    done
fi
[ "${status}" -eq 0 ] || exit "${status}"

# The checkout last, as a path may hold spaces; ~ for the home directory.
where="${MISE_CONFIG_ROOT:-${PWD}}"
# shellcheck disable=SC2088  # a path to read, not to expand
case "${where}" in
    "${HOME}"/*) where="~/${where#"${HOME}"/}" ;;
esac
echo "$$ $(date +%s) ${MISE_TASK_NAME:--} ${where}" >"${lock}"
SEVERINO_HEAVY_HOLDER=$$
export SEVERINO_HEAVY_HOLDER

# uptime(1) ends on the 1, 5 and 15 minute averages on macOS and Linux alike.
load() { uptime | sed 's/.*load averages*: *//'; }

echo "heavy: the turn starts, load average $(load)" >&2
status=0
bounded "$@" || status=$?
echo "heavy: the turn ends, load average $(load)" >&2
exit "${status}"
