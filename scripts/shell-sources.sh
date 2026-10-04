#!/bin/sh
# Every shell file in the repository, one per line: a tracked file whose first
# line names a shell, as a shebang or as a shellcheck directive (a sourced
# library has no shebang). What the gates parse and shellcheck.
set -eu
cd "$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)"
git ls-files | while IFS= read -r file; do
    [ -f "${file}" ] || continue
    if head -n 1 "${file}" 2>/dev/null |
        grep -qE '^(#!.*(/|env )(sh|bash|dash|ksh)([[:space:]]|$)|# shellcheck shell=)'; then
        printf '%s\n' "${file}"
    fi
done
