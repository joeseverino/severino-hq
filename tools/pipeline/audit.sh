#!/bin/sh
# No known vulnerability or adverse project status in a project's locked
# dependencies, every group included.
#   tools/pipeline/audit.sh [PROJECT_DIR]
#
# With a PROJECT_DIR, an extension package's uv.lock, through the host's
# pinned uv: its admission has no mise. With none, the host (`mise run
# lint:audit`): uv's audit of uv.lock, which alone reads a package's adverse
# status (deprecated, quarantined), then osv-scanner over every lockfile git
# tracks. osv-scanner reads each lock's osv-scanner.toml beside it, and judges
# a Go advisory by whether the controller calls the vulnerable code (it needs
# go on PATH for that, and without it reports the advisory as called). Both
# query their advisory service, so the audit fails offline.
set -eu
unset CDPATH

if [ "$#" -gt 0 ]; then
    exec uv audit --project "$1" --locked --preview-features audit-command
fi

host="$(cd -- "$(dirname -- "$0")/../.." && pwd)"
status=0
uv audit --project "$host" --locked --preview-features audit-command || status=1

# git's list goes through a file so a failure stops the audit, and each path
# reaches osv-scanner as one argument.
listed="$(mktemp -d)"
trap 'rm -rf "$listed"' EXIT
git -C "$host" ls-files -z -- ':(glob)**/uv.lock' ':(glob)**/package-lock.json' ':(glob)**/go.mod' >"$listed/z"
tr '\0' '\n' <"$listed/z" >"$listed/locks"
set --
while IFS= read -r lock; do
    set -- "$@" --lockfile="$host/$lock"
done <"$listed/locks"
osv-scanner scan "$@" || status=1
exit "$status"
