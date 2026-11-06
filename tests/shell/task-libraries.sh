# shellcheck shell=sh
# Sourced by a suite that runs a file task by its path in a copy of the
# checkout: the libraries the task sources, read from its own
# `. ./tools/lib/<name>` lines and theirs, so a library a task begins to
# source needs no list kept beside the suite.
#   copy_libraries <repo> <copy> <file in the repo>
copy_libraries() {
    while read -r line; do
        case "${line}" in
            ". ./tools/lib/"*) library="${line#. ./tools/lib/}" ;;
            *) continue ;;
        esac
        library="${library%%[[:space:]]*}"
        [ ! -f "$2/tools/lib/${library}" ] || continue
        mkdir -p "$2/tools/lib"
        cp "$1/tools/lib/${library}" "$2/tools/lib/"
        copy_libraries "$1" "$2" "tools/lib/${library}"
    done <"$1/$3"
}
