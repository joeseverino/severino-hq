# shellcheck shell=sh
# The heavy turn for a file task that weighs its own run, taken in one line
# before the work it weighs:
#   . ./tools/lib/heavy-turn.sh
# A heavy run runs the task again through `mise run turn`, which holds the
# lock while it runs; inside it, and inside any task that holds the lock,
# MISE_TASK_LOCKS names it and the body goes ahead. A task whose weight is
# fixed takes the lock in its own header instead (#MISE lock="heavy").
#
# Whether a run of tests is heavy is decided in one place, tools/gate/turn.py:
# a task that runs manage.py test on what it was given sets `test_arguments` to
# those arguments first, shell-quoted as mise hands them over, and a narrow
# run goes ahead outside the turn. `dev:look`, whose weight is the pages it
# names, sets `light_run` instead. With no argument the run is the whole suite.
# The task sets `turn_bound`, how long its heavy run may hold the turn.
case ",${MISE_TASK_LOCKS:-}," in
    *,heavy,*) ;;
    *)
        if [ -n "${test_arguments:-}" ] && [ -z "${light_run:-}" ] &&
            eval "uv run python -m tools.gate.turn ${test_arguments}"; then
            light_run=1
        fi
        if [ -z "${light_run:-}" ]; then
            # mise gives the turn task its own parse, dropping this task's
            # usage_* variables, so the run in the turn is handed them again.
            # Each name is a shell name, matched before it is expanded.
            set -- "$0" "$@"
            for name in $(env | sed -n 's/^\(usage_[A-Za-z0-9_]*\)=.*/\1/p'); do
                eval "set -- \"${name}=\${${name}}\" \"\$@\""
            done
            HQ_TURN_BOUND="${turn_bound:?}" exec mise run turn -- env "$@"
        fi
        ;;
esac
