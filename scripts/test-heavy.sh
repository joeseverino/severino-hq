#!/bin/sh
# Heavy commands take turns: a second one is refused and names the first, a
# killed holder frees the turn, a heavy command inside a turn runs, and CI takes
# no lock.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
heavy="${repo}/tools/lib/heavy.sh"
work="$(mktemp -d)"
holder=""
cleanup() {
    [ -z "${holder}" ] || kill "${holder}" 2>/dev/null || true
    rm -rf "${work}"
}
trap cleanup EXIT
unset CI SEVERINO_HEAVY_HOLDER
XDG_CACHE_HOME="${work}/cache"
export XDG_CACHE_HOME
lock="${XDG_CACHE_HOME}/severino/heavy.lock"

fail() {
    echo "test-heavy: $*" >&2
    exit 1
}

# Starts a holder and returns once it holds the turn: it writes to the fifo
# only from inside the command.
hold() {
    rm -f "${work}/ready"
    mkfifo "${work}/ready"
    # shellcheck disable=SC2016  # expanded by the inner shell
    "${heavy}" sh -c 'echo up >"$1"; exec sleep 60' sh "${work}/ready" &
    holder=$!
    read -r _ <"${work}/ready"
}

hold
status=0
said="$("${heavy}" true 2>&1)" || status=$?
[ "${status}" -eq 75 ] || fail "a second heavy command exited ${status}, not 75"
case "${said}" in
    *"pid ${holder},"*sleep*) ;;
    *) fail "the refusal does not name the holder (pid ${holder}): ${said}" ;;
esac

status=0
SEVERINO_HEAVY_HOLDER="${holder}" "${heavy}" true 2>/dev/null || status=$?
[ "${status}" -eq 75 ] || fail "a process outside the turn borrowed it by naming the holder"

status=0
CI=true "${heavy}" true || status=$?
[ "${status}" -eq 0 ] || fail "under CI a heavy command waited for the lock"

kill -KILL "${holder}"
wait "${holder}" 2>/dev/null || true
holder=""
"${heavy}" true || fail "a killed holder kept the turn"

# shellcheck disable=SC2016  # expanded by the inner shell
said="$("${heavy}" sh -c '"$1" echo nested' sh "${heavy}")" || fail "a heavy command inside a turn was refused"
[ "${said}" = nested ] || fail "the nested command did not run: ${said}"

rm -rf "${XDG_CACHE_HOME}"
CI=true "${heavy}" true
[ ! -e "${lock}" ] || fail "under CI a lock file was made"
