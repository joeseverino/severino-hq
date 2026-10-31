#!/bin/sh
# Heavy commands take turns: a second one names the first and waits for it, a
# bound that runs out exits 75 naming the holder, a killed command frees the
# turn, a heavy command inside a turn runs, a file task's body runs once, a
# waiting one names the holder again at its interval, a turn prints the load
# average as it starts and ends, a run of tests takes it only when it is
# heavy, CI takes no lock, every heavy task carries a bound
# that, run out, stops what it started and frees the turn, and two heavy tasks
# asked together queue on the turn however long the second waits.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
heavy="${repo}/tools/lib/heavy.sh"
work="$(mktemp -d)"
holder=""
hung=""
cleanup() {
    [ -z "${holder}" ] || kill "${holder}" 2>/dev/null || true
    [ -z "${hung}" ] || kill "${hung}" 2>/dev/null || true
    rm -rf "${work}"
}
trap cleanup EXIT
unset CI SEVERINO_HEAVY_HOLDER SEVERINO_HEAVY_TASK SEVERINO_HEAVY_WAIT MISE_TASK_PGID_MANAGED
XDG_CACHE_HOME="${work}/cache"
SEVERINO_HEAVY_BOUND=10m
export XDG_CACHE_HOME SEVERINO_HEAVY_BOUND
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
        *"mise run example-holder in "*", for "[0-9]*"m"[0-9]*"s (pid ${holder})"*) ;;
        *) fail "$2 does not name the holder (pid ${holder}): $1" ;;
    esac
}

for unbound in "" 0 soon 2h30m; do
    status=0
    SEVERINO_HEAVY_BOUND="${unbound}" "${heavy}" true 2>/dev/null || status=$?
    [ "${status}" -eq 64 ] || fail "a heavy command bounded by '${unbound}' exited ${status}, not 64"
    status=0
    CI=true SEVERINO_HEAVY_BOUND="${unbound}" "${heavy}" true 2>/dev/null || status=$?
    [ "${status}" -eq 64 ] || fail "under CI a heavy command bounded by '${unbound}' exited ${status}, not 64"
done

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
case "${said}" in
    *" 600s"*" in the background."*) ;;
    *) fail "the waiting line does not tell an agent to run a gate that may wait in the background: ${said}" ;;
esac
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

# A file task takes the turn by running itself again under heavy.sh, and its
# body goes ahead in the run whose pid heavy-turn.sh exported, so a command
# inside a turn runs as heavy.sh's own pid: a fork there would start the task
# again without end. This one counts its starts and gives up past the three a
# turn takes.
cat >"${work}/file-task" <<'SH'
#!/bin/sh
set -eu
starts="$(($(cat "$1" 2>/dev/null || echo 0) + 1))"
echo "${starts}" >"$1"
[ "${starts}" -le 3 ] || exit 9
. "${MISE_CONFIG_ROOT}/tools/lib/heavy-turn.sh"
echo ran >>"$2"
SH
chmod +x "${work}/file-task"
MISE_CONFIG_ROOT="${repo}" "${work}/file-task" "${work}/starts" "${work}/ran" 2>/dev/null ||
    fail "a file task that takes the turn started itself $(cat "${work}/starts") times"
[ "$(cat "${work}/ran")" = ran ] || fail "a file task that takes the turn did not run its body once"

# A run of tests takes the turn only when tools/gate/turn.py finds it heavy:
# the test task and a file task alike run tests a label narrows while another
# command holds the turn, and wait for it with every test of a tree.
hold
SEVERINO_HEAVY_WAIT=0 mise -C "${repo}" run -q test hq.platform.core.tests.test_test_runner.BrowserTestTests \
    >"${work}/light" 2>&1 || { cat "${work}/light" >&2; fail "a narrow test run waited for the turn, or failed"; }
SEVERINO_HEAVY_WAIT=0 mise -C "${repo}" run -q lint:source tools.gate.tests.test_turn.NarrowRunTests \
    >"${work}/light" 2>&1 || { cat "${work}/light" >&2; fail "a narrow source test run waited for the turn, or failed"; }
status=0
SEVERINO_HEAVY_WAIT=0 mise -C "${repo}" run -q test hq.platform.core.tests >/dev/null 2>&1 || status=$?
[ "${status}" -eq 75 ] || fail "a test run of a whole package exited ${status} while the turn was held, not 75"
status=0
SEVERINO_HEAVY_WAIT=0 mise -C "${repo}" run -q lint:source >/dev/null 2>&1 || status=$?
[ "${status}" -eq 75 ] || fail "every source test exited ${status} while the turn was held, not 75"
echo go >"${work}/release"
wait "${holder}" || fail "the holder failed"
holder=""

rm -rf "${XDG_CACHE_HOME}"
CI=true "${heavy}" true
[ ! -e "${lock}" ] || fail "under CI a lock file was made"

# Every task that takes the turn declares its bound and carries no mise
# timeout, whose clock runs while it waits: a TOML task through heavy.sh as
# its shell, a file task by sourcing heavy-turn.sh, and fast and test, which
# take it from inside their run. heavy.sh refuses a turn without a bound, so a
# missing one fails on its first run; this finds it before.
mise -C "${repo}" tasks ls --all --hidden --json >"${work}/tasks.json"
heavies="$(jq '[.[] | select((.shell // "") | startswith("tools/lib/heavy.sh"))] | length' "${work}/tasks.json")"
[ "${heavies}" -gt 1 ] || fail "found ${heavies} tasks that take the turn through heavy.sh"
jq -r '.[] | select(((.shell // "") | startswith("tools/lib/heavy.sh")) or .name == "fast" or .name == "test") | .name' \
    "${work}/tasks.json" >"${work}/heavy-names"
jq -r '.[] | select(.file != null) | .file' "${work}/tasks.json" >"${work}/files"
while IFS= read -r file; do
    if grep -q '^\. .*tools/lib/heavy-turn\.sh' "${file}"; then
        jq -r --arg file "${file}" '.[] | select(.file == $file) | .name' "${work}/tasks.json" >>"${work}/heavy-names"
    fi
done <"${work}/files"
while IFS= read -r name; do
    jq -e --arg name "${name}" '.[] | select(.name == $name) | .timeout == null' "${work}/tasks.json" >/dev/null ||
        fail "the heavy task ${name} carries a mise timeout, which would stop it while it waits: declare SEVERINO_HEAVY_BOUND instead"
    jq -e --arg name "${name}" '.[] | select(.name == $name) | [.env[]? | select(startswith("SEVERINO_HEAVY_BOUND=")) | ltrimstr("SEVERINO_HEAVY_BOUND=")] | (last // "") | test("^[0-9]+[smh]?$")' \
        "${work}/tasks.json" >/dev/null || fail "the heavy task ${name} declares no SEVERINO_HEAVY_BOUND"
done <"${work}/heavy-names"

# A heavy task that hangs past its bound is stopped at it, what it started
# with it, and a waiter takes the turn: run by hand, and from inside another
# task. The waiter's own bound is the hung task's and a margin.
# heavy.sh runs under the shell this suite runs under, which lint:shell
# makes dash, Linux's, as well: dash grants no job control without a terminal,
# and a CI runner has none.
shell="${SHELL_UNDER_TEST:-/bin/sh}"
bound="${work}/bound"
mkdir -p "${bound}"
mkfifo "${bound}/never"
cat >"${bound}/mise.toml" <<TOML
[task_templates.heavy]
shell = "${shell} ${heavy} sh -o errexit -c"
env = { SEVERINO_HEAVY_BOUND = "1s" }

[tasks.hang]
extends = "heavy"
run = 'echo \$\$ >ready; read -r _ <never'

[tasks.outer]
run = "mise run hang"

[tasks.first]
extends = "heavy"
run = 'echo ran >first'

[tasks.second]
extends = "heavy"
run = 'echo ran >second'
TOML
for task in hang outer; do
    rm -f "${bound}/ready"
    mkfifo "${bound}/ready"
    MISE_TRUSTED_CONFIG_PATHS="${bound}" mise -C "${bound}" run "${task}" >/dev/null 2>&1 &
    run=$!
    read -r hung <"${bound}/ready"
    SEVERINO_HEAVY_WAIT=30 "${heavy}" true 2>/dev/null || fail "run as ${task} under ${shell}, a hung heavy task kept the turn past its bound"
    status=0
    wait "${run}" || status=$?
    [ "${status}" -ne 0 ] || fail "run as ${task} under ${shell}, a heavy task stopped at its bound passed"
    if kill -0 "${hung}" 2>/dev/null; then
        fail "run as ${task} under ${shell}, the hung command outlived its bound"
    fi
    hung=""
done

# Two heavy tasks asked together queue on the turn: each, bounded at a
# second, waits past that while another command holds the turn, then both run,
# since a bound starts with the turn rather than the task.
rm -f "${bound}/first" "${bound}/second"
mkfifo "${bound}/together"
hold
MISE_TRUSTED_CONFIG_PATHS="${bound}" SEVERINO_HEAVY_EVERY=1 mise -C "${bound}" run first ::: second \
    >"${bound}/together" 2>&1 &
run=$!
exec 7<"${bound}/together"
waited=""
while IFS= read -r said <&7; do
    case "${said}" in
        *"still waiting after 0m0"[2-9]"s"*) waited=yes; break ;;
    esac
done
[ -n "${waited}" ] || fail "two heavy tasks asked together did not wait past their bound for the turn"
echo go >"${work}/release"
wait "${holder}" || fail "the holder failed"
holder=""
cat <&7 >"${bound}/rest"
exec 7<&-
status=0
wait "${run}" || status=$?
[ "${status}" -eq 0 ] || { cat "${bound}/rest" >&2; fail "two heavy tasks asked together exited ${status}"; }
for task in first second; do
    [ "$(cat "${bound}/${task}")" = ran ] || fail "${task} of two heavy tasks asked together did not run"
done
