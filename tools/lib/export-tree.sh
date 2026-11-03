# shellcheck shell=sh
# The checkout's files as git sees them (tracked and new, ignored ones left
# out) copied into a directory. A tracked file deleted from the work tree is
# skipped, as a build context would skip it. Names pass NUL-separated, since
# git quotes a name holding a backslash or a newline in a line-separated list,
# and tar reads a --null list verbatim.
#   export_tree DEST [PATHSPEC...]
export_tree() {
    dest=$1
    shift
    git ls-files -z --cached --others --exclude-standard -- "$@" |
        perl -0 -ne 'chomp; print "$_\0" if -f' | tar --null -cf - -T - | tar -xf - -C "${dest}"
}

# A scanner's work directory, printed, with a fresh export of the checkout in
# its src/. It is under the checkout's var/, so a worktree takes its scans with
# it when it is removed. The scanner removes src/ and what it built from it on
# exit, keeping only the report it prints from.
#   work="$(scan_tree NAME)"
scan_tree() {
    work="${PWD}/var/scan/$1"
    rm -rf "${work}/src" &&
        mkdir -p "${work}/src" &&
        export_tree "${work}/src" &&
        printf '%s\n' "${work}"
}
