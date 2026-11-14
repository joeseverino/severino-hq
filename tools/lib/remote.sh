# shellcheck shell=sh
# The line a gate a person types takes first, before any work of its own:
#   . ./tools/lib/remote.sh
# With a second machine set (git config hq.gateRemote, which every worktree of
# the repository shares however it was made; HQ_GATE_REMOTE overrides it, and
# empty keeps a run here), the task runs on whichever machine has the tokens
# it takes free now, that machine first, and on that machine this ends with
# its status; when the machine does not answer, it says so in one line. A run
# here goes on, as do the tasks it runs, which HQ_GATE_HERE tells not to ask
# again. A task that weighs its run sets `turn_share` first, by the line the
# machine that runs it reads again; one that sets none takes every token
# (tools/gate/remote.py; docs/Development.md, Gates on a second machine).
if [ -z "${HQ_GATE_REMOTE+set}" ]; then
    HQ_GATE_REMOTE=$(git config --get hq.gateRemote || true)
    export HQ_GATE_REMOTE
fi
if [ -n "${HQ_GATE_REMOTE:-}" ] && [ -z "${HQ_GATE_HERE:-}" ]; then
    remote_status=0
    uv run --locked python -m tools.gate.remote "${turn_share:-all}" || remote_status=$?
    [ "${remote_status}" -eq 75 ] || exit "${remote_status}"
    HQ_GATE_HERE=1
    export HQ_GATE_HERE
fi
