# shellcheck shell=sh
# The checkout's files as git sees them (tracked and new, ignored ones left
# out) copied into a directory. A tracked file deleted from the work tree is
# skipped, as a build context would skip it.
#   export_tree DEST [PATHSPEC...]
export_tree() {
    dest=$1
    shift
    git -c core.quotepath=off ls-files --cached --others --exclude-standard -- "$@" |
        while IFS= read -r file; do
            [ -f "${file}" ] && printf '%s\n' "${file}"
        done | tar -cf - -T - | tar -xf - -C "${dest}"
}
