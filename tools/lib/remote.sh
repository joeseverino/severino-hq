# shellcheck shell=sh
# The line a gate a person types takes first, before any work of its own:
#   . ./tools/lib/remote.sh
# With HQ_GATE_REMOTE set (the gitignored mise.local.toml), the task runs on
# that machine and this ends with its status; when the machine does not
# answer, it says so in one line and the task goes on here, as do the tasks it
# runs, which HQ_GATE_HERE tells not to ask again (tools/gate/remote.py;
# docs/Development.md, Gates on a second machine).
if [ -n "${HQ_GATE_REMOTE:-}" ] && [ -z "${HQ_GATE_HERE:-}" ]; then
    remote_status=0
    uv run --locked python -m tools.gate.remote || remote_status=$?
    [ "${remote_status}" -eq 75 ] || exit "${remote_status}"
    HQ_GATE_HERE=1
    export HQ_GATE_HERE
fi
