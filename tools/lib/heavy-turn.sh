# shellcheck shell=sh
# The heavy turn for a file task, taken in one line before its work:
#   . ./tools/lib/heavy-turn.sh
# Every heavy file task sources it. One whose run takes the whole machine
# whatever it is given takes the lock in its own header
# (#MISE lock=["gate", "heavy"]); one that weighs its own run sets
# `turn_bound`, how long its run may hold the turn or a share, and runs again
# in the whole turn (`mise run turn`) or in a share of it
# (tools/gate/turn.py --share). Inside a task that holds the lock,
# MISE_TASK_LOCKS names it, and the body goes ahead under tools/lib/measured,
# which ends the run with the line on what its process tree spent; inside a
# share, HQ_TURN_SHARE says so, and the body goes ahead as it is, measured by
# the share. A task that sets no `turn_bound` and holds no turn was run by its
# path, outside mise, and goes ahead as it is.
#
# What a run of tests weighs is decided in one place, tools/gate/turn.py: a
# task that runs manage.py test on what it was given sets `test_arguments` to
# those arguments first, shell-quoted as mise hands them over. One whose run
# is one process whatever it is given (`dev:look`) sets `turn_share` to its
# tokens instead. With neither, the run takes the whole turn.
case ",${MISE_TASK_LOCKS:-}," in
    *,heavy,*)
        [ -n "${HQ_MEASURED:-}" ] || exec ./tools/lib/measured "$0" "$@"
        ;;
    *)
        if [ -n "${turn_bound+set}" ] && [ -z "${HQ_TURN_SHARE:-}" ]; then
            if [ -n "${test_arguments:-}" ] && [ -z "${turn_share:-}" ]; then
                # Its last line is the weight; a run that cannot be weighed takes the whole turn.
                turn_share="$(eval "uv run python -m tools.gate.turn ${test_arguments}" | tail -n 1)"
                case "${turn_share}" in
                    [1-9]) ;;
                    *) turn_share="" ;;
                esac
            fi
            if [ -n "${turn_share:-}" ]; then
                exec uv run python -m tools.gate.turn --share "${turn_share}" "${turn_bound}" "$0" "$@"
            fi
            # mise gives the turn task its own parse, dropping this task's
            # usage_* variables, so the run in the turn is handed them again.
            # Each name is a shell name, matched before it is expanded.
            set -- "$0" "$@"
            for name in $(env | sed -n 's/^\(usage_[A-Za-z0-9_]*\)=.*/\1/p'); do
                eval "set -- \"${name}=\${${name}}\" \"\$@\""
            done
            HQ_TURN_BOUND="${turn_bound}" exec mise run turn -- env "$@"
        fi
        ;;
esac
