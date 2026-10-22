#!/bin/sh
# Heavy commands take turns: a second one names the first and waits for it, a
# bound that runs out exits 75 naming the holder, a killed command frees the
# turn, a heavy command inside a turn runs, a waiting one names the holder
# again at its interval, a turn prints the load average as it starts and ends,
# and CI takes no lock.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
heavy="${repo}/tools/lib/heavy.sh"
work="$(mktemp -d)"
holder=""
cleanup() {
    [ -z "${holder}" ] || kill "${holder}" 2>/dev/null || true
    rm -rf "${work}"
}
trap cleanup EXIT
unset CI SEVERINO_HEAVY_HOLDER SEVERINO_HEAVY_WAIT
XDG_CACHE_HOME="${work}/cache"
export XDG_CACHE_HOME
lock="${XDG_CACHE_HOME}/severino/heavy.lock"

fail() {
    echo "test-heavy: $*" >&2
    exit 1
}

fifo() {
    rm -f "${work}/$1"
    mkfifo "${work}/$1"
}

# Starts a holder and returns once it holds the turn: its command writes its
# pid to the ready fifo, then holds until a line reaches release.
hold() {
    fifo ready
    fifo release
    # shellcheck disable=SC2016  # expanded by the inner shell
    MISE_TASK_NAME=example-holder "${heavy}" sh -c 'echo $$ >"$1"; read -r _ <"$2"' sh "${work}/ready" "${work}/release" 2>/dev/null &
    holder=$!
    read -r command <"${work}/ready"
}

names_holder() {
    case "$1" in
        *"pid ${holder}, task example-holder, holding the turn "[0-9]*"m"[0-9]*"s, running "*release*) ;;
        *) fail "$2 does not name the holder (pid ${holder}): $1" ;;
    esac
}

hold
status=0
said="$(SEVERINO_HEAVY_WAIT=0 "${heavy}" true 2>&1)" || status=$?
[ "${status}" -eq 75 ] || fail "a heavy command whose bound ran out exited ${status}, not 75"
names_holder "${said}" "the expired wait"

status=0
SEVERINO_HEAVY_HOLDER="${holder}" SEVERINO_HEAVY_WAIT=0 "${heavy}" true 2>/dev/null || status=$?
[ "${status}" -eq 75 ] || fail "a process outside the turn borrowed it by naming the holder"

status=0
CI=true "${heavy}" true || status=$?
[ "${status}" -eq 0 ] || fail "under CI a heavy command waited for the lock"

# A second heavy command says whom it waits for, says it again at its
# interval, then runs once the first ends.
fifo waiting
# shellcheck disable=SC2016  # expanded by the inner shell
SEVERINO_HEAVY_EVERY=1 "${heavy}" sh -c 'echo ran >"$1"' sh "${work}/second" 2>"${work}/waiting" &
second=$!
exec 8<"${work}/waiting"
read -r said <&8 || fail "the second heavy command did not say it was waiting"
names_holder "${said}" "the waiting line"
read -r said <&8 || fail "the second heavy command did not say it was still waiting"
case "${said}" in
    "heavy: still waiting after 0m0"[1-9]"s, held by "*) ;;
    *) fail "the repeated waiting line does not say how long it has waited: ${said}" ;;
esac
names_holder "${said}" "the repeated waiting line"
[ ! -e "${work}/second" ] || fail "the second heavy command ran while the first held the turn"
echo go >"${work}/release"
wait "${holder}" || fail "the holder failed"
holder=""
wait "${second}" || fail "the waiting heavy command failed once the turn was free"
exec 8<&-
[ "$(cat "${work}/second")" = ran ] || fail "the waiting heavy command did not run"

hold
kill -KILL "${command}"
wait "${holder}" 2>/dev/null || true
holder=""
SEVERINO_HEAVY_WAIT=0 "${heavy}" true 2>/dev/null || fail "a killed command kept the turn"

said="$("${heavy}" true 2>&1)" || fail "a heavy command failed"
case "${said}" in
    *"the turn starts, load average "[0-9]*"the turn ends, load average "[0-9]*) ;;
    *) fail "a turn did not print the load average as it started and ended: ${said}" ;;
esac

# shellcheck disable=SC2016  # expanded by the inner shell
said="$("${heavy}" sh -c '"$1" echo nested' sh "${heavy}" 2>/dev/null)" || fail "a heavy command inside a turn was refused"
[ "${said}" = nested ] || fail "the nested command did not run: ${said}"

rm -rf "${XDG_CACHE_HOME}"
CI=true "${heavy}" true
[ ! -e "${lock}" ] || fail "under CI a lock file was made"
