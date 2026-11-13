#!/bin/sh
# Heavy tasks take turns on mise's task lock `heavy`: across two worktrees a
# second waits for the first and names it, one that gives up names the holder
# too, a killed holder frees the turn, a task's timeout starts once it holds
# the turn, a heavy task inside a turn runs at once, a task that weighs its
# own run (tools/lib/heavy-turn.sh) runs its body once and inside the turn
# when it is heavy, under its own bound, and beside the holder when it is
# light, and a run of tests
# takes the turn only when tools/gate/turn.py finds it heavy. Each heavy run,
# by a template's shell, a header's lock or the turn task, ends with one line
# (tools/lib/measured) whose CPU holds what its descendants spent and
# whose peak holds each role's processes held at once, a test worker apart from
# its children; a heavy run inside one adds none, and a heavy file task run by
# its path, outside mise, runs as itself.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
holder=""
command=""
cleanup() {
    [ -z "${holder}" ] || kill "${holder}" 2>/dev/null || true
    [ -z "${command}" ] || kill "${command}" 2>/dev/null || true
    rm -rf "${work}"
}
trap cleanup EXIT
# Run under lint:shell, this suite is inside that task's turn, which every
# mise it starts would inherit and so never wait, and inside its measured run,
# which every run would take as its own.
unset MISE_TASK_LOCKS MISE_TASK_LOCK_TIMEOUT HQ_MEASURED
# The uv this checkout pins, for the test runs made from a directory whose
# config names none, where a shim finds no version.
uv="$(mise -C "${repo}" which uv)"

fail() {
    echo "test-heavy: $*" >&2
    exit 1
}

fifo() {
    rm -f "${work}/$1"
    mkfifo "${work}/$1"
}

# Two worktrees of one repository, each with the turn task and the line a
# task that weighs its run sources, as this checkout has them.
main="${work}/main"
second="${work}/second"
mkdir -p "${main}/mise-tasks" "${main}/tools/lib"
cp "${repo}/mise-tasks/turn" "${main}/mise-tasks/"
cp "${repo}/tools/lib/heavy-turn.sh" "${repo}/tools/lib/measured" "${main}/tools/lib/"
cat >"${main}/mise.toml" <<'TOML'
[task_templates.heavy]
lock = "heavy"
shell = "tools/lib/measured sh -o errexit -c"

[tasks.hold]
extends = "heavy"
timeout = "10m"
run = 'echo $$ >"$READY"; read -r _ <"$RELEASE"'

[tasks.quick]
extends = "heavy"
timeout = "1s"
run = 'echo ran >>"$RAN"'

[tasks.outer]
extends = "heavy"
timeout = "1m"
run = "mise run quick"

[tasks.spend]
extends = "heavy"
timeout = "2m"
run = '"$UV" run --project "$REPO" --locked python manage.py test'
TOML
# A test run as Django's parallel runner makes one: two workers, each holding
# 32 MB and running a child, as a walk runs apart, that holds 32 MB and spends
# half a second of CPU. The child holds its memory for longer than the
# measurer's first two samples apart.
cat >"${main}/manage.py" <<'PY'
import multiprocessing
import subprocess
import sys

CHILD = """
import time
held = b"\\x01" * (32 << 20)
while time.process_time() < 0.5:
    pass
time.sleep(3)
"""


def work():
    held = b"\x01" * (32 << 20)
    subprocess.run([sys.executable, "-c", CHILD], check=True)
    del held


if __name__ == "__main__":
    workers = [multiprocessing.Process(target=work) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    sys.exit(max(worker.exitcode for worker in workers))
PY
# A task whose weight is fixed, as this checkout's take the turn in a header.
cat >"${main}/mise-tasks/fixed" <<'SH'
#!/bin/sh
#MISE lock="heavy"
#MISE timeout="1m"
set -eu
. ./tools/lib/heavy-turn.sh
echo "${MISE_TASK_LOCKS:-outside}" >>"$RAN"
SH
chmod +x "${main}/mise-tasks/fixed"
# It counts its starts and gives up past the two a heavy run takes, so a
# body that ran itself again without end fails rather than hangs.
cat >"${main}/mise-tasks/weighed" <<'SH'
#!/bin/sh
#USAGE arg "[word]"
set -eu
starts="$(($(cat "$STARTS" 2>/dev/null || echo 0) + 1))"
echo "${starts}" >"$STARTS"
[ "${starts}" -le 2 ] || exit 9
turn_bound="${BOUND:-1m}"
. ./tools/lib/heavy-turn.sh
echo "${MISE_TASK_LOCKS:-outside}" >>"$RAN"
echo "${usage_word:-}" >"$WORD"
[ -z "${LINGER:-}" ] || read -r _ <"$LINGER"
SH
chmod +x "${main}/mise-tasks/weighed"
git -C "${main}" init -q
git -C "${main}" add -A
git -C "${main}" -c user.name=test -c user.email=test@example.com -c commit.gpgsign=false commit -q --no-verify -m fixture
git -C "${main}" worktree add -q "${second}"

# The locks live in the state directory, so this suite's are its own, and
# this checkout's tasks run in it too.
# shellcheck source=tests/shell/mise-state.sh
. "${repo}/tests/shell/mise-state.sh"
mise_trust "${main}:${second}:${repo}" "${work}/mise-state"
RAN="${work}/ran"
STARTS="${work}/starts"
READY="${work}/ready"
RELEASE="${work}/release"
WORD="${work}/word"
export RAN STARTS READY RELEASE WORD

# Starts a holder in the main worktree and returns once its command holds the
# turn; the command holds it until a line reaches release.
hold() {
    fifo ready
    fifo release
    mise -C "${main}" run hold >/dev/null 2>&1 &
    holder=$!
    read -r command <"${READY}"
}

release() {
    echo go >"${RELEASE}"
    wait "${holder}" || fail "the holder failed"
    holder=""
    command=""
}

names_holder() {
    case "$1" in
        *"held by hold in "*"/main (pid ${holder}, for "*) ;;
        *) fail "$2 does not name the holder, hold in the main worktree (pid ${holder}): $1" ;;
    esac
}

# A heavy task in the other worktree waits and says for whom. Its output is a
# fifo read through fd 8 as it comes. While it waits, a third one that gives
# up names the holder, and outlasts the waiter's own one-second timeout,
# which only starts once the waiter holds the turn.
hold
fifo waiting
rm -f "${RAN}"
mise -C "${second}" run quick >"${work}/waiting" 2>&1 &
waiter=$!
exec 8<"${work}/waiting"
said=""
while IFS= read -r line <&8; do
    case "${line}" in
        *"waiting for lock heavy"*) said="${line}"; break ;;
    esac
done
[ -n "${said}" ] || fail "a heavy task in another worktree did not say it was waiting for the turn"
names_holder "${said}" "the waiting line"
[ ! -e "${RAN}" ] || fail "a heavy task ran while another worktree held the turn"
status=0
said="$(MISE_TASK_LOCK_TIMEOUT=2s mise -C "${second}" run quick 2>&1)" || status=$?
[ "${status}" -ne 0 ] || fail "a heavy task that gave up on the turn passed"
names_holder "${said}" "a heavy task that gave up waiting"
release
told=""
while IFS= read -r line <&8; do told="${told} ${line}"; done
exec 8<&-
status=0
wait "${waiter}" || status=$?
[ "${status}" -eq 0 ] || fail "a heavy task that waited longer than its timeout was stopped, so its timeout ran while it waited: ${told}"
case "${told}" in
    *"got lock heavy after "*) ;;
    *) fail "a task that waited did not say for how long, which a gate's closing line reads: ${told}" ;;
esac
[ "$(cat "${RAN}")" = ran ] || fail "the heavy task that waited did not run once"

# A holder killed outright frees the turn at once, whatever it started.
hold
kill -KILL "${holder}"
wait "${holder}" 2>/dev/null || true
holder=""
MISE_TASK_LOCK_TIMEOUT=5s mise -C "${second}" run quick >/dev/null 2>&1 || fail "a killed holder kept the turn"
kill "${command}" 2>/dev/null || true
command=""

# The lines a run printed on what a heavy run's tree spent.
measured() {
    grep -E '^(\[[^]]*\] )*heavy: [0-9.hms]+ wall, [0-9.hms]+ CPU, load average [0-9. ]+(, peak memory .*)?; sampling took [0-9.hms]+ CPU$' "$1" || true
}

# A heavy run's own line, once whatever runs inside it.
one_line() {
    [ "$(measured "$1" | grep -c '')" -eq 1 ] || fail "$2 did not end with one line on what it spent: $(cat "$1")"
}

# The size a line gives after <pattern> (a role, or "one at most"), in MB.
size() {
    printf '%s\n' "$1" | sed -nE "s/.*$2 ([0-9.]+) (MB|GB).*/\1 \2/p" | awk '{ print int($2 == "GB" ? $1 * 1024 : $1) }'
}

# A heavy task run from inside one goes ahead in its turn, and its line is the outer one's.
MISE_TASK_LOCK_TIMEOUT=5s mise -C "${main}" run outer >"${work}/out" 2>&1 || fail "a heavy task inside a turn waited on it"
one_line "${work}/out" "a heavy task that ran another"

# A run of tests in the turn: its CPU holds the half second each child spent,
# and its peak names the test run, two workers of 32 MB or more each and two
# children apart from them.
REPO="${repo}" UV="${uv}" mise -C "${main}" run spend >"${work}/out" 2>&1 || { cat "${work}/out" >&2; fail "a measured run of tests failed"; }
one_line "${work}/out" "a run of tests"
line="$(measured "${work}/out")"
cpu="$(printf '%s\n' "${line}" | sed -nE 's/.* wall, ([0-9.]+)s CPU,.*/\1/p')"
awk -v cpu="${cpu:-0}" 'BEGIN { exit !(cpu >= 1.0) }' || fail "the CPU its children spent is not in the line: ${line}"
case "${line}" in
    *"gate "*", test run "*) ;;
    *) fail "the line does not name the gate and the test run apart: ${line}" ;;
esac
[ "$(size "${line}" 'workers')" -ge 64 ] || fail "two workers of 32 MB read as less: ${line}"
[ "$(size "${line}" 'workers [0-9.]+ [MG]B \(one at most')" -ge 32 ] || fail "a worker's own peak is not its 32 MB: ${line}"
[ "$(size "${line}" 'children')" -ge 64 ] || fail "two children of 32 MB under the workers read as less: ${line}"

# A wrapper above the real test run (python -m <module> ... manage.py test, as a
# verdict runs one) is the gate's, so the workers under the run are still workers.
cat >"${main}/wrapper.py" <<'PY'
import subprocess
import sys

sys.exit(subprocess.run(sys.argv[1:], check=False).returncode)
PY
(cd "${main}" && ./tools/lib/measured python3 -m wrapper "${uv}" run --project "${repo}" --locked python manage.py test) \
    >"${work}/out" 2>&1 || { cat "${work}/out" >&2; fail "a measured run of tests under a wrapper failed"; }
one_line "${work}/out" "a run of tests under a wrapper"
line="$(measured "${work}/out")"
[ "$(size "${line}" 'workers')" -ge 64 ] || fail "the workers under a wrapped test run are not counted as workers: ${line}"

# The line reports and never judges: the command's exit status, or the signal
# that ended it, is the run's, and a sample it could not take is named in the
# line while that status stands. This ps fails once the command waits on it.
mkdir "${work}/broken"
cat >"${work}/broken/ps" <<'SH'
#!/bin/sh
echo sampled >"$SAMPLED"
exit 1
SH
chmod +x "${work}/broken/ps"
fifo sampled
for case in "exit 7:7" "kill -TERM \$\$:143" "read -r _ <\"\$SAMPLED\"; exit 3:3"; do
    status=0
    (cd "${main}" && SAMPLED="${work}/sampled" PATH="${work}/broken:${PATH}" ./tools/lib/measured sh -c "${case%:*}") \
        >"${work}/out" 2>&1 || status=$?
    [ "${status}" -eq "${case##*:}" ] || fail "a command that ran '${case%:*}' left the measurer with ${status}"
    one_line "${work}/out" "a command that ran '${case%:*}'"
done
grep -q 'peak memory unread: a sample failed' "${work}/out" || fail "a sample that failed was not named: $(cat "${work}/out")"

# A task whose weight is fixed, by its header, ends with the line; run by its
# path, outside mise, it holds no turn and runs as itself.
rm -f "${RAN}"
MISE_TASK_LOCK_TIMEOUT=5s mise -C "${main}" run fixed >"${work}/out" 2>&1 || fail "a heavy file task failed: $(cat "${work}/out")"
one_line "${work}/out" "a heavy file task"
(cd "${main}" && ./mise-tasks/fixed) >"${work}/out" 2>&1 || fail "a heavy file task run by its path failed: $(cat "${work}/out")"
[ -z "$(measured "${work}/out")" ] || fail "a heavy file task run by its path, outside the turn, was measured"
[ "$(cat "${RAN}")" = "heavy
outside" ] || fail "a heavy file task ran other than once in the turn, and once by its path outside it: $(cat "${RAN}")"

# A heavy run of a task that weighs its own runs its body once, inside the
# turn, on the arguments it was given, and ends with one line; a light one
# runs beside the holder, and a heavy one waits for it.
rm -f "${RAN}" "${STARTS}"
MISE_TASK_LOCK_TIMEOUT=5s mise -C "${main}" run weighed "two words" >"${work}/out" 2>&1 ||
    fail "a heavy run of a task that weighs its own failed after $(cat "${STARTS}") starts"
one_line "${work}/out" "a heavy run of a task that weighs its own"
[ "$(cat "${RAN}")" = gate,heavy ] || fail "a heavy run's body ran outside the turn, or not once: $(cat "${RAN}")"
[ "$(cat "${WORD}")" = "two words" ] || fail "a heavy run's body lost the arguments it was given: '$(cat "${WORD}")'"
# The heavy run is stopped at the bound its task sets, not the turn task's;
# the whole run's own limit only ends a run that bound did not stop.
fifo never
rm -f "${RAN}" "${STARTS}"
status=0
said="$(BOUND=1s LINGER="${work}/never" mise -C "${main}" run --timeout 30s weighed 2>&1)" || status=$?
[ "${status}" -ne 0 ] || fail "a heavy run that outlived its task's bound was not stopped"
case "${said}" in
    *"[turn] ERROR timed out after 1s"*) ;;
    *) fail "a heavy run was not stopped at the bound its task set: ${said}" ;;
esac
hold
rm -f "${RAN}" "${STARTS}"
light_run=1 MISE_TASK_LOCK_TIMEOUT=1s mise -C "${second}" run weighed >/dev/null 2>&1 ||
    fail "a light run of a task that weighs its own waited for the turn"
[ "$(cat "${RAN}")" = outside ] || fail "a light run's body ran other than once, outside the turn: $(cat "${RAN}")"
status=0
said="$(MISE_TASK_LOCK_TIMEOUT=1s mise -C "${second}" run weighed 2>&1)" || status=$?
[ "${status}" -ne 0 ] || fail "a heavy run of a task that weighs its own ran beside the holder"
names_holder "${said}" "a heavy run that gave up waiting"

# This checkout's test task and a file task alike run tests a label narrows
# beside the holder, and wait for it with every test of a tree.
MISE_TASK_LOCK_TIMEOUT=1s mise -C "${repo}" run -q test hq.platform.core.tests.test_test_runner.BrowserTestTests \
    >"${work}/light" 2>&1 || { cat "${work}/light" >&2; fail "a narrow test run waited for the turn, or failed"; }
MISE_TASK_LOCK_TIMEOUT=1s mise -C "${repo}" run -q lint:source tools.gate.tests.test_turn.NarrowRunTests \
    >"${work}/light" 2>&1 || { cat "${work}/light" >&2; fail "a narrow source test run waited for the turn, or failed"; }
one_line "${work}/light" "a narrow source test run, outside the turn,"
for task in "test hq.platform.core.tests" lint:source; do
    status=0
    # shellcheck disable=SC2086  # the task and its label, as typed
    said="$(MISE_TASK_LOCK_TIMEOUT=1s mise -C "${repo}" run -q ${task} 2>&1)" || status=$?
    [ "${status}" -ne 0 ] || fail "mise run ${task} ran beside the holder"
    names_holder "${said}" "mise run ${task}"
done
release
