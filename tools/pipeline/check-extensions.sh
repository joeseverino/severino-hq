#!/usr/bin/env bash
# Every extension's admission checks (mise-tasks/plugin/check), run against
# this checkout's host.
#   tools/pipeline/check-extensions.sh SET INSTALLER
# SET is the composition set; each extension is read from
# extensions/<repository name>/<path>, as Compose checked it out. INSTALLER is
# the image the checks run in, holding none of the job's credentials. Every
# extension is checked before this fails, so one run names all that broke.
#
# This runs in a public log: each check's output goes to
# $WITHHELD_DIR/extension-check-<id>.log, which is sealed, and the log counts.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 SET INSTALLER" >&2; exit 2; }
set_file=$1 installer=$2
scratch="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
withheld="${WITHHELD_DIR:-$scratch}"
mkdir -p "$withheld"

host="$scratch/host"
mkdir -p "$host"
git archive HEAD | tar -x -C "$host"
chmod -R a+rwX "$host"

failed=0
total=0
while IFS=$'\t' read -r id repository package; do
  total=$((total + 1))
  # A copy, so a coordinated build after this reads the branch as cloned
  # rather than as a check left it.
  work="$scratch/check-$total"
  cp -a "extensions/$(basename "$repository")" "$work"
  if docker run --rm --network bridge --user 10001:10001 \
    --env HOME=/tmp --env UV_PYTHON_DOWNLOADS=never --env PLUGIN_REPOSITORY="$repository" \
    --volume "$host:/host" --volume "$work:/src" \
    --entrypoint sh "$installer" \
    /host/mise-tasks/plugin/check --plugin-root "/src/$package" \
    >"$withheld/extension-check-$id.log" 2>&1; then
    echo "extension $total: passed"
  else
    failed=$((failed + 1))
    echo "extension $total: failed"
  fi
done < <(jq -r '.extensions[] | [.id, .repository, (.path // ".")] | @tsv' "$set_file")

if [ "$failed" -gt 0 ]; then
  echo "::error title=An extension fails this host::$failed of $total extensions fail their checks against this pull request's host. Which, and why, is in this run's sealed failure logs."
  exit 1
fi
