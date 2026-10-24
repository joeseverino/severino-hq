# shellcheck shell=sh
# A heavy file task's turn, taken in one line before anything else:
#   . ./tools/lib/heavy-turn.sh
# A file task cannot set a shell, so this runs the task again under heavy.sh,
# which holds the turn while it runs; the pid it exports marks the run whose
# body goes ahead, so the body runs once. A task
# in another language keeps a shell file task in front that takes the turn and
# then execs it, since `uv run` starts its interpreter under a new pid.
#
# Whether a run of tests is heavy is decided in one place, tools/gate/turn.py:
# a task that runs manage.py test on what it was given sets `test_arguments` to
# those arguments first, shell-quoted as mise hands them over, and a narrow
# run goes ahead outside the turn. `dev:look`, whose weight is the pages it
# names, sets `light_run` instead. With no argument the run is the whole suite.
if [ -z "${light_run:-}" ] && [ -n "${test_arguments:-}" ] && [ "${SEVERINO_HEAVY_TASK:-}" != "$$" ]; then
    if eval "uv run python -m tools.gate.turn ${test_arguments}"; then
        light_run=1
    fi
fi
if [ -z "${light_run:-}" ] && [ "${SEVERINO_HEAVY_TASK:-}" != "$$" ]; then
    SEVERINO_HEAVY_TASK=$$
    export SEVERINO_HEAVY_TASK
    exec "${MISE_CONFIG_ROOT}/tools/lib/heavy.sh" "$0" "$@"
fi
