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

# A scanner's work directory, printed, with a fresh export of the checkout in
# its src/. It is under the checkout's var/, so a worktree takes its scans with
# it when it is removed.
#   work="$(scan_tree NAME)"
scan_tree() {
    work="${PWD}/var/scan/$1"
    rm -rf "${work}/src" &&
        mkdir -p "${work}/src" &&
        export_tree "${work}/src" &&
        printf '%s\n' "${work}"
}
