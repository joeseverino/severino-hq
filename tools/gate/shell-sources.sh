#!/bin/sh
# Every shell file in the repository, one per line: a file git tracks or would
# track (untracked, not ignored), so a new script is checked before its commit,
# whose first line names a shell, as a shebang or as a shellcheck directive (a
# sourced library has no shebang). What the gates parse and shellcheck. Run
# from the checkout's root, as every caller is. Given paths, it prints those of
# them that are shell files instead, so fast asks it about a change alone.
set -eu

shell_file() {
    [ -f "${1}" ] || return 1
    head -n 1 "${1}" 2>/dev/null |
        grep -qE '^(#!.*(/|env )(sh|bash|dash|ksh)([[:space:]]|$)|# shellcheck shell=)'
}

if [ "$#" -gt 0 ]; then
    for file in "$@"; do
        if shell_file "${file}"; then
            printf '%s\n' "${file}"
        fi
    done
    exit 0
fi
# Names pass NUL-separated, since git quotes a name holding a backslash in a
# list of lines, and through a file, so a failed listing fails the lister.
listed="$(mktemp)"
trap 'rm -f "${listed}"' EXIT
git ls-files -z --cached --others --exclude-standard >"${listed}"
tr '\0' '\n' <"${listed}" | while IFS= read -r file; do
    if shell_file "${file}"; then
        printf '%s\n' "${file}"
    fi
done
