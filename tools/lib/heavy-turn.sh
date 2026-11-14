# shellcheck shell=sh
# The heavy turn for a file task, taken in one line before its work:
#   . ./tools/lib/heavy-turn.sh
# Every heavy file task sources it. One that needs the whole machine whatever
# it is given takes the lock in its own header (#MISE lock=["gate", "heavy"]);
# one that weighs its own run sets `turn_bound`, how long its run may hold its
# share, and runs again in a share of the machine's tokens
# (tools/gate/turn.py --share), which hands it this task's usage_* variables
# with the rest of its environment. Inside a task that holds the lock, or a
# share, MISE_TASK_LOCKS names it, and the body goes ahead under
# tools/lib/measured, which ends the run with the line on what its process
# tree spent. A task that sets no `turn_bound` and holds no turn was run by its
# path, outside mise, and goes ahead as it is.
#
# What a run of tests weighs is decided in one place, tools/gate/turn.py: a
# task that runs manage.py test on what it was given sets `test_arguments` to
# those arguments first, shell-quoted as mise hands them over. One whose run
# does not depend on its tests sets `turn_share` to its tokens instead, or to
# `all` for a run that needs every token (a Docker VM).
case ",${MISE_TASK_LOCKS:-}," in
    *,heavy,*)
        [ -n "${HQ_MEASURED:-}" ] || exec ./tools/lib/measured "$0" "$@"
        ;;
    *)
        if [ -n "${turn_bound+set}" ]; then
            if [ -z "${turn_share:-}" ]; then
                # Its last line is the weight; a run that cannot be weighed takes every token.
                turn_share="$(eval "uv run python -m tools.gate.turn ${test_arguments:?}" | tail -n 1)"
                case "${turn_share}" in
                    '' | *[!0-9]*) turn_share=all ;;
                esac
            fi
            exec uv run python -m tools.gate.turn --share "${turn_share}" "${turn_bound}" "$0" "$@"
        fi
        ;;
esac
