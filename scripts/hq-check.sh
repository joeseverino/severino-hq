#!/usr/bin/env bash
# Open or move one of HQ's checks on a commit, as HQ's app: the one row that
# says where the change stands, updated in place as each stage finishes.
#
#   scripts/hq-check.sh NAME SHA STATUS TITLE SUMMARY [CONCLUSION]
#
# STATUS is queued, in_progress or completed; CONCLUSION goes with completed.
# Optional environment: CHECK_TEXT (the details, markdown), CHECK_URL (where
# "details" links). GH_TOKEN is HQ's app token, with checks: write.
#
# The check still in progress on the commit is moved on; a finished one is
# left as the record of that run, and a new one opened beside it (a redeploy
# of the same commit), which is the one GitHub then shows.
set -euo pipefail

name="$1" sha="$2" status="$3" title="$4" summary="$5" conclusion="${6:-}"
readonly repo="${GITHUB_REPOSITORY:?}"

open="$(gh api "repos/${repo}/commits/${sha}/check-runs?check_name=$(jq -rn --arg n "${name}" '$n|@uri')&filter=latest" \
  --jq '.check_runs | map(select(.status != "completed")) | first | .id // empty')"

jq -n \
  --arg name "${name}" --arg sha "${sha}" --arg status "${status}" --arg conclusion "${conclusion}" \
  --arg title "${title}" --arg summary "${summary}" --arg text "${CHECK_TEXT:-}" --arg url "${CHECK_URL:-}" \
  '{name: $name, head_sha: $sha, status: $status,
    output: ({title: $title, summary: $summary} + (if $text == "" then {} else {text: $text} end))}
   + (if $conclusion == "" then {} else {conclusion: $conclusion} end)
   + (if $url == "" then {} else {details_url: $url} end)' \
  | if [ -n "${open}" ]; then
      gh api -X PATCH "repos/${repo}/check-runs/${open}" --input - >/dev/null
    else
      gh api -X POST "repos/${repo}/check-runs" --input - >/dev/null
    fi
