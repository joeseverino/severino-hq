#!/bin/sh
# Runs a heavy command (a suite, a browser gate, a demo build) only while no
# other one runs on this machine, so two never share its memory:
#   tools/lib/heavy.sh <command...>
#
# A command is heavy when it starts a process per core or a VM: a parallel
# suite (Django's workers, every shell suite at once, Go's race-detector
# tests), a browser per core, Docker's VM, CodeQL's database. Each holds a
# gigabyte or more. One process, or a tool and its few helpers, holds at most
# about 600 MB, near a development server: a Django command on an empty
# database, a serial test run, mypy, a linter. It runs beside the holder, and
# an aggregate holds no turn between its gates, so a light step never waits.
# A task takes the turn for its heavy steps alone.
#
# A TOML task takes it as its shell (the heavy template in mise.toml); a file
# task, which cannot set one, sources tools/lib/heavy-turn.sh.
#
# The turn is an exclusive flock(2) lock on one file every checkout and
# worktree shares. While another command holds it, this names the holder and
# waits for the turn, naming it again every SEVERINO_HEAVY_EVERY seconds (a
# minute), so a holder that has stalled is seen rather than read as this one
# hanging; past SEVERINO_HEAVY_WAIT seconds (two hours, the longest a heavy
# task may run) it names the holder once more and exits 75 (EX_TEMPFAIL).
#
# A hung holder is stopped by its own bound, never by a waiter, since it may be
# another session's work. SEVERINO_HEAVY_BOUND (`30m`, `2h`, `90s`), which every
# heavy task declares in its env, is how long the command may run once its turn
# starts, and this refuses a turn without one. The bound is this script's, not
# mise's task timeout: that clock starts before the wait, so two heavy tasks
# asked together (`mise run browser:layout-1 ::: browser:layout-2`) would see
# the second killed as it waited rather than queued, and a heavy task carries
# none (tests/shell/test-heavy.sh).
#
# The lock lasts as long as this script, which waits for its command, so a
# command that ends or is stopped frees it. A heavy
# command started inside a turn runs at once: the holder exports its pid, and a
# process descended from it is inside that turn, under the turn's bound. A CI
# runner runs one job, so there the command runs at once, under its bound.
#
# A turn prints the machine's load average as it starts and as it ends, so a
# gate's time can be read against what else the machine was doing, and at its
# end the peak memory the command's processes held at once (`sample`), so each
# heavy run measures the line above; a CI run prints the end alone.
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

# seconds <duration>: 90, 90s, 30m or 2h as seconds, or nothing for anything else.
seconds() {
    case "$1" in
        '' | *[!0-9smh]* | [!0-9]* | *[smh]?*) return 0 ;;
        *s) echo "${1%s}" ;;
        *m) echo "$((${1%m} * 60))" ;;
        *h) echo "$((${1%h} * 3600))" ;;
        *) echo "$1" ;;
    esac
}

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
# stops that whole group when its bound passes or this script is stopped. A
# signal to a task run from inside another task reaches only the pid mise
# started, so without the group a stop there would leave what the command
# started running. Perl's setpgrp makes the group: set -m is the shell's to
# refuse, and dash, Linux's /bin/sh, does without a terminal. Until perl has
# made it, the trap reaches perl by its pid. The command never holds the
# lock's descriptor: the turn lasts exactly as long as this script, which a
# SIGKILL ends whatever the command does. A command inside a turn runs as this
# pid (heavy-turn.sh knows its own run by it), in the turn's group, so the
# turn's bound reaches it.
#
# The bound is a watcher beside the command: once SEVERINO_HEAVY_BOUND has
# passed it says so and stops the command's group, killing what outlasts ten
# seconds, and it goes when the command ends first.
#
# A sampler beside it reads the memory the command's whole tree holds at once
# (its descendants, and its group's members a parent left behind) and keeps
# the largest sum, which `peak` reads once the command ends. The platforms
# keep a high-water mark only per process (time -l, VmHWM, a cgroup
# that holds the CI runner itself), never for a tree, so this samples. The
# reading is what each platform calls a process's own share: footprint(1)'s
# phys_footprint on macOS, the figure Activity Monitor shows and memory
# pressure reacts to, and Linux's proportional set size with its swapped
# share, so pages the workers share count once. A sample comes every two
# seconds, or twenty times as long as the last one's CPU took, so the sampler
# holds at most a twentieth of a core; a spike that rises and falls between
# two samples, or a process that lives less than one interval, is missed.
# It writes the peak once it is told to stop, and goes on its own when this
# script is gone.
sample() {
    exec perl -e '
        BEGIN { $SIG{TERM} = sub { $main::stop = 1 } }
        use strict;
        use warnings;
        use Time::HiRes ();
        our $stop;
        my ($root, $out) = @ARGV;
        my $parent = getppid;
        my $linux = -r "/proc/self/smaps_rollup";
        exit 0 unless $linux || -x "/usr/bin/footprint";
        open STDERR, ">", "/dev/null";
        sub tree {
            my (%children, @todo, %seen);
            my $note = sub {
                my ($pid, $ppid, $pgid) = @_;
                push @{ $children{$ppid} }, $pid;
                push @todo, $pid if $pgid == $root;
            };
            if ($linux) {
                opendir my $proc, "/proc" or return ();
                for my $pid (grep { /^\d+$/ } readdir $proc) {
                    open my $stat, "<", "/proc/$pid/stat" or next;
                    my $line = <$stat>;
                    next unless defined $line;
                    $note->($pid, (split " ", substr $line, rindex($line, ")") + 2)[1, 2]);
                }
            } else {
                open my $ps, "-|", "ps", "-A", "-o", "pid=,ppid=,pgid=" or return ();
                $note->(split) while <$ps>;
            }
            push @todo, $root;
            while (@todo) {
                my $pid = pop @todo;
                next if $seen{$pid}++;
                push @todo, @{ $children{$pid} || [] };
            }
            return keys %seen;
        }
        sub held {
            my $bytes = 0;
            if ($linux) {
                for my $pid (@_) {
                    open my $rollup, "<", "/proc/$pid/smaps_rollup" or next;
                    while (<$rollup>) { $bytes += $1 * 1024 if /^(?:Swap)?Pss:\s+(\d+) kB/ }
                }
            } elsif (@_) {
                open my $fp, "-|", "/usr/bin/footprint", "--noCategories", "-f", "bytes", map { ("-p", $_) } @_
                    or return 0;
                while (<$fp>) { $bytes += $1 if /\[\d+\]:.*?Footprint: (\d+) B/ }
            }
            return $bytes;
        }
        my $peak = 0;
        until ($stop) {
            my $before = 0;
            $before += $_ for times;
            my $now = held(tree());
            $peak = $now if $now > $peak;
            my $spent = -$before;
            $spent += $_ for times;
            exit 0 if getppid != $parent;
            my $pause = 20 * $spent;
            Time::HiRes::sleep($pause > 2 ? $pause : 2) unless $stop;
        }
        my $mib = $peak / 1048576;
        exit 0 unless $peak;
        open my $file, ">", $out or exit 0;
        printf $file $mib >= 1024 ? "%.2f GB\n" : "%d MB\n", $mib >= 1024 ? $mib / 1024 : $mib;
    ' "$@"
}

bounded() {
    peaks="$(mktemp "${TMPDIR:-/tmp}/severino-heavy.XXXXXX")"
    perl -e 'setpgrp; exec { $ARGV[0] } @ARGV; warn "heavy: $ARGV[0]: $!\n"; exit 127' -- "$@" 9>&- &
    job=$!
    perl -e '
        my ($limit, $said, $group) = @ARGV;
        sleep $limit;
        warn "heavy: the command ran past its bound of $said (SEVERINO_HEAVY_BOUND), so it is stopped\n";
        kill "TERM", -$group;
        for (1 .. 10) { sleep 1; exit 0 unless kill 0, -$group; }
        kill "KILL", -$group;
    ' "${limit}" "${SEVERINO_HEAVY_BOUND}" "${job}" 9>&- &
    watcher=$!
    sample "${job}" "${peaks}" 9>&- &
    sampler=$!
    trap 'kill -s TERM -- "-${job}" 2>/dev/null || kill -s TERM "${job}" 2>/dev/null || true' HUP INT TERM
    ended=0
    wait "${job}" || ended=$?
    # A trapped signal ends the wait early; the job is reaped once it exits.
    while kill -0 "${job}" 2>/dev/null; do
        ended=0
        wait "${job}" || ended=$?
    done
    kill "${watcher}" "${sampler}" 2>/dev/null || true
    wait "${watcher}" 2>/dev/null || true
    wait "${sampler}" 2>/dev/null || true
    return "${ended}"
}

# uptime(1) ends on the 1, 5 and 15 minute averages on macOS and Linux alike.
load() { uptime | sed 's/.*load averages*: *//'; }

# The peak the sampler left in the file `bounded` made for it, as the end of
# a line, or nothing when it had none: a command that ended before the
# sampler read it, or a platform with neither footprint(1) nor /proc.
peak() {
    held=""
    read -r held <"${peaks}" 2>/dev/null || true
    rm -f "${peaks}"
    [ -z "${held}" ] || printf ', peak memory %s' "${held}"
}

if inside_turn; then
    exec "$@"
fi
limit="$(seconds "${SEVERINO_HEAVY_BOUND:-}")"
if [ -z "${limit}" ] || [ "${limit}" -eq 0 ]; then
    echo "heavy: SEVERINO_HEAVY_BOUND is how long the command may run once its turn starts (30m, 2h, 90s), not '${SEVERINO_HEAVY_BOUND:-}': declare it in the task's env" >&2
    exit 64
fi
# unlocked <command...>: the command under its bound with no lock to take, as
# the holder of a turn, so a heavy command it starts runs inside it.
unlocked() {
    SEVERINO_HEAVY_HOLDER=$$
    export SEVERINO_HEAVY_HOLDER
    status=0
    bounded "$@" || status=$?
    echo "heavy: the command ends, load average $(load)$(peak)" >&2
    exit "${status}"
}

if [ -n "${CI:-}" ]; then
    unlocked "$@"
fi
# take <seconds>: holds the lock on descriptor 9, or exits 75 once the seconds
# run out. Perl's flock is flock(2) on the descriptor this script holds, so
# the lock outlives perl and lasts as long as this script. The wait is a
# blocking flock the kernel sleeps on, ended by an alarm: macOS lockf(1) given
# a timeout polls the lock instead, spending a core for as long as it waits,
# and flock(1) is Linux's alone.
take() {
    perl -MFcntl=:flock -e '
        my $seconds = shift;
        open my $turn, ">&=", 9 or die "heavy: descriptor 9: $!\n";
        if ($seconds == 0) {
            exit 0 if flock $turn, LOCK_EX | LOCK_NB;
            exit 75 if $!{EWOULDBLOCK};
        } else {
            $SIG{ALRM} = sub { exit 75 };
            alarm $seconds;
            exit 0 if flock $turn, LOCK_EX;
            exit 75 if $!{EINTR};
        }
        die "heavy: the turn: $!\n";
    ' "$1"
}

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
asked="$(date +%s)"
take 0 || status=$?
if [ "${status}" -eq 75 ]; then
    # A wait can outlast an agent's tool call, which then goes on in the
    # background; the first line keeps the agent from starting a second copy.
    echo "heavy: waiting for the turn, held by $(describe_holder). A call that reaches its timeout keeps running in the background: wait for its notification, never start a second copy." >&2
    waited=0
    queued=1
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

# A gate's closing line reads this one (tools/gate/verdict.py:WAITED).
[ -z "${queued:-}" ] || echo "heavy: waited $(($(date +%s) - asked))s for the turn" >&2
echo "heavy: the turn starts, load average $(load)" >&2
status=0
bounded "$@" || status=$?
echo "heavy: the turn ends, load average $(load)$(peak)" >&2
exit "${status}"
