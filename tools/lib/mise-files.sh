# shellcheck shell=sh
# The mise files checks:mise-format holds and fix writes: mise.toml and every
# task file mise reads from mise-tasks/. `mise fmt` finds the config files in a
# directory, never the task files, so the list is made here, once, for both.
# Written to a file, so a failed find stops the caller rather than handing it
# an empty list.
#   mise_files FILE
mise_files() {
    echo mise.toml >"$1"
    find mise-tasks -name '*.toml' >>"$1"
}
