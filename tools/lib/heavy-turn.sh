# shellcheck shell=sh
# A heavy file task's turn, taken in one line before anything else:
#   . ./tools/lib/heavy-turn.sh
# A file task cannot set a shell, so this runs the task again under heavy.sh,
# which holds the turn while it runs; the pid it exports marks the run whose
# body goes ahead, so the body runs once. A task
# in another language keeps a shell file task in front that takes the turn and
# then execs it, since `uv run` starts its interpreter under a new pid.
if [ "${SEVERINO_HEAVY_TASK:-}" != "$$" ]; then
    SEVERINO_HEAVY_TASK=$$
    export SEVERINO_HEAVY_TASK
    exec "${MISE_CONFIG_ROOT}/tools/lib/heavy.sh" "$0" "$@"
fi
