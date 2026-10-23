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
# holder whose processes together use next to no CPU (under 1% of a core) for
# SEVERINO_HEAVY_IDLE seconds (ten minutes) is hung rather than working: this
# names it, how long it has been idle and the kill that stops it, and exits 75
# then, so a gate fails at once rather than waiting out the two hours behind
# it. Nothing here stops the holder, which may be another session's work. The
# lock is taken on a descriptor the command inherits, so
# it lasts as long as the command and what it started, and a killed command
# frees it. A heavy command started inside a turn runs at once: the holder
# exports its pid, and a process descended from it is inside that turn. A CI
# runner runs one job, and a system with neither lockf(1) nor flock(1) has no
# lock to take: both run the command directly.
#
# A turn prints the machine's load average as it starts and as it ends, so a
# gate's time can be read against what else the machine was doing.
set -eu

[ "$#" -gt 0 ] || { echo "usage: tools/lib/heavy.sh <command...>" >&2; exit 64; }

bound="${SEVERINO_HEAVY_WAIT:-7200}"
every="${SEVERINO_HEAVY_EVERY:-60}"
idle="${SEVERINO_HEAVY_IDLE:-600}"
case "${bound}" in
    '' | *[!0-9]*) echo "heavy: SEVERINO_HEAVY_WAIT is a number of seconds, not ${bound}" >&2; exit 64 ;;
esac
case "${idle}" in
    '' | 0 | *[!0-9]*) echo "heavy: SEVERINO_HEAVY_IDLE is a positive number of seconds, not ${idle}" >&2; exit 64 ;;
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

# Seconds as minutes and seconds: 1203 is 20m03s.
span() { printf '%dm%02ds' "$(($1 / 60))" "$(($1 % 60))"; }

# The holder writes its pid, the time it took the turn and its mise task into
# the file; if that pid is gone while the lock is still held, a process it
# started holds it, and lsof finds which.
describe_holder() {
    pid="" since="" task=""
    read -r pid since task <"${lock}" 2>/dev/null || true
    held=""
    case "${since}" in
        '' | *[!0-9]*) ;;
        *) held=", holding the turn $(span "$(($(date +%s) - since))")" ;;
    esac
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        pid="$(lsof -t -- "${lock}" 2>/dev/null | grep -vx "$$" | head -n 1 || true)"
        task=""
    fi
    if [ -n "${pid}" ] && ps -p "${pid}" >/dev/null 2>&1; then
        printf 'pid %s, task %s%s, running %s: %s' "${pid}" "${task:-unnamed}" "${held}" \
            "$(ps -o etime= -p "${pid}" | tr -d ' ')" "$(ps -o command= -p "${pid}")"
    else
        printf 'a process lsof cannot name (%s)' "${lock}"
    fi
}

# The pid holding the turn: the one the file names while it runs, else the
# one lsof finds holding the file. Nothing when neither names a live process.
holder_pid() {
    pid=""
    read -r pid _ <"${lock}" 2>/dev/null || true
    if [ -z "${pid}" ] || ! ps -p "${pid}" >/dev/null 2>&1; then
        pid="$(lsof -t -- "${lock}" 2>/dev/null | grep -vx "$$" | head -n 1 || true)"
    fi
    [ -n "${pid}" ] && ps -p "${pid}" >/dev/null 2>&1 && printf '%s' "${pid}"
}

# holder_tree <pid>: the CPU time, in hundredths of a second, the pid and every
# process under it have used, then their pids. macOS's -S adds each exited
# child's time to its parent's, so a holder working through short commands is
# not read as idle; a ps without it is read without it. ps reports a process's
# time as [dd-][hh:]mm:ss[.hh], macOS with hundredths and procps without.
holder_tree() {
    { ps -A -S -o pid= -o ppid= -o time= 2>/dev/null || ps -A -o pid= -o ppid= -o time=; } | awk -v root="$1" '
        function hundredths(t,    days, parts, n, i, s) {
            days = 0
            if (index(t, "-")) { days = substr(t, 1, index(t, "-") - 1); t = substr(t, index(t, "-") + 1) }
            n = split(t, parts, ":")
            s = 0
            for (i = 1; i <= n; i++) s = s * 60 + parts[i]
            return int((days * 86400 + s) * 100 + 0.5)
        }
        { parent[$1] = $2; used[$1] = hundredths($3) }
        END {
            found[root] = 1
            for (grew = 1; grew; ) {
                grew = 0
                for (pid in parent) if (!(pid in found) && (parent[pid] in found)) { found[pid] = 1; grew = 1 }
            }
            total = 0; pids = ""
            for (pid in found) if (pid in used) { total += used[pid]; pids = pids " " pid }
            print total pids
        }'
}

exec 9>>"${lock}"
status=0
take 0 || status=$?
if [ "${status}" -eq 75 ]; then
    echo "heavy: waiting for the turn, held by $(describe_holder)" >&2
    waited=0
    # The quiet window: since when, and the CPU the holder's processes had used then.
    quiet_from=0 quiet_used="" quiet_holder=""
    while :; do
        step="${every}"
        [ "$((bound - waited))" -ge "${step}" ] || step="$((bound - waited))"
        status=0
        take "${step}" || status=$?
        [ "${status}" -eq 75 ] || break
        waited="$((waited + step))"
        if [ "${waited}" -ge "${bound}" ]; then
            echo "heavy: the turn is still held after ${bound}s, by $(describe_holder). Stop it if it is stuck." >&2
            exit 75
        fi
        holder="$(holder_pid || true)" used="" tree=""
        if [ -n "${holder}" ]; then
            read -r used tree <<EOF
$(holder_tree "${holder}")
EOF
        fi
        # Busy is a hundredth of a CPU second or more for each second of the
        # window: 1% of a core. A holder that changed or is gone opens a new window.
        if [ -z "${used}" ] || [ -z "${quiet_used}" ] || [ "${holder}" != "${quiet_holder}" ] \
            || [ "$((used - quiet_used))" -ge "$((waited - quiet_from))" ]; then
            quiet_from="${waited}" quiet_used="${used}" quiet_holder="${holder}"
        elif [ "$((waited - quiet_from))" -ge "${idle}" ]; then
            echo "heavy: the turn's holder has used no CPU for $(span "$((waited - quiet_from))"), so it is hung, not working: $(describe_holder). Stop it with: kill ${tree}" >&2
            exit 75
        fi
        echo "heavy: still waiting after $(span "${waited}"), held by $(describe_holder)" >&2
    done
fi
[ "${status}" -eq 0 ] || exit "${status}"

echo "$$ $(date +%s) ${MISE_TASK_NAME:-}" >"${lock}"
SEVERINO_HEAVY_HOLDER=$$
export SEVERINO_HEAVY_HOLDER

# uptime(1) ends on the 1, 5 and 15 minute averages on macOS and Linux alike.
load() { uptime | sed 's/.*load averages*: *//'; }

echo "heavy: the turn starts, load average $(load)" >&2
status=0
"$@" || status=$?
echo "heavy: the turn ends, load average $(load)" >&2
exit "${status}"
