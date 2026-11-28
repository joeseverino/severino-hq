#!/bin/sh
# A measured run of tests (tools/lib/measured) that stops making progress
# where no test process's stall watch can see it, here a process hanging on
# its way out with a child alive, is stopped once neither a test began nor
# its tree spent CPU for twice the test stall bound: the line names the test
# it hung in, the hung process dumps its stacks, no process of its tree is
# left, and it exits 124. A run whose tests keep beginning, one that spends
# CPU without a test beginning, and a run that holds no test run are never
# stopped, nor the gate once its test run has ended.
set -eu

repo="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
cleanup() {
    if [ -f "${work}/pids" ]; then
        read -r hung child <"${work}/pids" || true
        kill -KILL "${hung:-}" "${child:-}" 2>/dev/null || true
    fi
    rm -rf "${work}"
}
trap cleanup EXIT
# Run under lint:shell, this suite is inside that task's measured run, which
# each run here would take as its own.
unset HQ_MEASURED HQ_TEST_MARKS
# A run is stopped after 2s without progress, a test's own watch after 1s.
HQ_TEST_STALL_SECONDS=1
export HQ_TEST_STALL_SECONDS

fail() {
    echo "test-stall: $*" >&2
    exit 1
}

# A test run with the stall watch: it marks a test inside the watch, then
# per MODE hangs after it with a child alive, keeps marking tests with no
# CPU between them, or spends CPU without marking one.
cat >"${work}/manage.py" <<'PY'
import os
import subprocess
import sys
import threading
import time

from hq.platform.core.stalls import stall_watch


def hang_on_the_way_out():
    threading.Event().wait()


mode = os.environ["MODE"]
with stall_watch.watching():
    stall_watch.mark("example.tests.Hung.test_forever")
    if mode == "marks":
        for n in range(12):
            time.sleep(0.5)
            stall_watch.mark(f"example.tests.Steady.test_{n}")
    elif mode == "busy":
        end = time.process_time() + 6
        while time.process_time() < end:
            pass
if mode == "hang":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
    with open(os.environ["PIDS"], "w") as pids:
        print(os.getpid(), child.pid, file=pids)
    hang_on_the_way_out()
PY

# The gate goes on for the seconds given once its test run has ended, as uv
# does for a moment and a verdict for longer.
measured() { # <mode> [<seconds>]
    status=0
    # shellcheck disable=SC2016  # the inner shell expands its arguments
    (cd "${work}" && MODE="$1" PIDS="${work}/pids" PYTHONPATH="${repo}" "${repo}/tools/lib/measured" \
        sh -c 'uv run --project "$1" --locked python manage.py test && sleep "$2"' sh "${repo}" "${2:-0}") >"${work}/out" 2>&1 || status=$?
}

measured hang
[ "${status}" -eq 124 ] || fail "a hung run of tests ended with ${status}, not 124: $(cat "${work}/out")"
read -r hung child <"${work}/pids"
grep -q "hung in example.tests.Hung.test_forever (pid ${hung}," "${work}/out" ||
    fail "the stop did not name the test the hung process was in: $(cat "${work}/out")"
grep -q "in hang_on_the_way_out" "${work}/out" || fail "the hung process did not dump its stacks: $(cat "${work}/out")"
for pid in "${hung}" "${child}"; do
    ! kill -0 "${pid}" 2>/dev/null || fail "process ${pid} of the stopped run is still alive"
done

for mode in marks busy; do
    measured "${mode}" 3
    [ "${status}" -eq 0 ] || fail "a run of tests making progress (${mode}), its gate going on after it, was stopped: $(cat "${work}/out")"
done

status=0
"${repo}/tools/lib/measured" sleep 5 >"${work}/out" 2>&1 || status=$?
[ "${status}" -eq 0 ] || fail "a run that holds no test run was stopped: $(cat "${work}/out")"
