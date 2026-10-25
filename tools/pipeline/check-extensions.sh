#!/usr/bin/env bash
# Every extension's admission checks (tools/pipeline/check-plugin.sh), run against
# this checkout's host.
#   tools/pipeline/check-extensions.sh SET INSTALLER
# SET is the composition set; each extension is read from
# extensions/<repository name>/<path>, as Compose checked it out. INSTALLER is
# the image the checks run in, holding none of the job's credentials. Every
# extension is checked before this fails, so one run names all that broke.
#
# The checks run at once, up to one per CPU: each is a single-threaded type
# check and test run, so more would only queue.
#
# This runs in a public log: each check's output goes to
# $WITHHELD_DIR/extension-check-<id>.log, which is sealed, and the log counts,
# in the set's order, once every check has finished.
set -euo pipefail

[ "$#" -eq 2 ] || { echo "usage: $0 SET INSTALLER" >&2; exit 2; }
set_file=$1 installer=$2
scratch="${RUNNER_TEMP:-${TMPDIR:-/tmp}}"
withheld="${WITHHELD_DIR:-$scratch}"
mkdir -p "$withheld"
slots=$(getconf _NPROCESSORS_ONLN)

host="$scratch/host"
mkdir -p "$host"
git archive HEAD | tar -x -C "$host"
chmod -R a+rwX "$host"

# check N ID REPOSITORY PACKAGE: the Nth extension's verdict goes to
# result-N, so the counts keep the set's order whichever finishes first.
check() {
  # A copy, so a coordinated build after this reads the branch as cloned
  # rather than as a check left it.
  local work="$scratch/check-$1" result=failed
  cp -a "extensions/$(basename "$3")" "$work"
  docker run --rm --network bridge --user 10001:10001 \
    --env HOME=/tmp --env UV_PYTHON_DOWNLOADS=never --env PLUGIN_REPOSITORY="$3" \
    --volume "$host:/host" --volume "$work:/src" \
    --entrypoint sh "$installer" \
    /host/tools/pipeline/check-plugin.sh --plugin-root "/src/$4" \
    >"$withheld/extension-check-$2.log" 2>&1 && result=passed
  echo "$result" >"$scratch/result-$1"
}
export -f check
export scratch withheld host installer
jq -r '.extensions | to_entries[] | [.key + 1, .value.id, .value.repository, (.value.path // ".")] | @tsv' "$set_file" |
  xargs -r -n 4 -P "$slots" bash -c 'check "$@"' check

failed=0
total=$(jq '.extensions | length' "$set_file")
for ((n = 1; n <= total; n++)); do
  result=$(cat "$scratch/result-$n" 2>/dev/null || echo failed)
  echo "extension $n: $result"
  [ "$result" = passed ] || failed=$((failed + 1))
done

if [ "$failed" -gt 0 ]; then
  echo "::error title=An extension fails this host::$failed of $total extensions fail their checks against this pull request's host. Which, and why, is in this run's sealed failure logs."
  exit 1
fi
