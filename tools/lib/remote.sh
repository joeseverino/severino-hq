# shellcheck shell=sh
# The line a gate a person types takes first, before any work of its own:
#   . ./tools/lib/remote.sh
# With a second machine set (git config hq.gateRemote, which every worktree of
# the repository shares however it was made; HQ_GATE_REMOTE overrides it, and
# empty keeps a run here), the task runs on that machine and this ends with its
# status; when the machine does not answer, it says so in one line and the task
# goes on here, as do the tasks it runs, which HQ_GATE_HERE tells not to ask
# again (tools/gate/remote.py; docs/Development.md, Gates on a second machine).
if [ -z "${HQ_GATE_REMOTE+set}" ]; then
    HQ_GATE_REMOTE=$(git config --get hq.gateRemote || true)
    export HQ_GATE_REMOTE
fi
if [ -n "${HQ_GATE_REMOTE:-}" ] && [ -z "${HQ_GATE_HERE:-}" ]; then
    remote_status=0
    uv run --locked python -m tools.gate.remote || remote_status=$?
    [ "${remote_status}" -eq 75 ] || exit "${remote_status}"
    HQ_GATE_HERE=1
    export HQ_GATE_HERE
fi
