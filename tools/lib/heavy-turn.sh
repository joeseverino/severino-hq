# shellcheck shell=sh
# A heavy file task's turn, taken in one line once it has set `repo` to the
# checkout, before anything else:
#   . "${repo}/tools/lib/heavy-turn.sh"
# A file task cannot set a shell, so this runs the task again under heavy.sh,
# as the same process: the pid it exports says the re-run has the turn.
if [ "${SEVERINO_HEAVY_TASK:-}" != "$$" ]; then
    SEVERINO_HEAVY_TASK=$$
    export SEVERINO_HEAVY_TASK
    exec "${repo:?set repo to the checkout first}/tools/lib/heavy.sh" "$0" "$@"
fi
