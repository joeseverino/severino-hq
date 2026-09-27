#!/usr/bin/env bash
# HQ's verdict on a pull request, as one check: ready to merge, or held, why,
# and what fixes it. Run by CI's Ready job once every other gate has finished.
#
# It waits for the same commit's CodeQL, and its Composition (compose.yml) when
# the pull request has one, so the verdict speaks for every workflow, reads the
# logs of whatever failed and asks scripts/diagnose.py why. It posts only job
# names and the catalog's own words, never a line of a log, so nothing private
# reaches this public repository.
#
# Environment:
#   GH_TOKEN      this run's token (actions: read), to read runs and logs
#   APP_TOKEN     HQ's app token (checks: write), to post the check
#   HEAD_SHA      the pull request's head commit
#   COMPOSED      "true" when the pull request is composed (same repository)
#   THIS_RUN      this run's id
#   RUN_URL       this run's page
set -euo pipefail

readonly check_name="Severino HQ · Review"
readonly repo="${GITHUB_REPOSITORY:?}"
readonly wait_seconds="${VERDICT_WAIT_SECONDS:-2400}"

# The gates of this run: every job Ready needs, by the name GitHub shows.
rows="$(gh api "repos/${repo}/actions/runs/${THIS_RUN}/jobs?per_page=100" \
  --jq '.jobs[] | select(.name != "Ready") | "\(.id)\t\(.name)\t\(.conclusion // "pending")"')"

# The same commit's other workflows, once each has finished: HQ, which composes
# it (only when the pull request is composed), and CodeQL.
others="CodeQL"
[ "${COMPOSED:-false}" = true ] && others="HQ CodeQL"
deadline=$(( $(date +%s) + wait_seconds ))
for workflow in ${others}; do
  found=""
  while :; do
    found="$(gh api "repos/${repo}/actions/runs?head_sha=${HEAD_SHA}&event=pull_request&per_page=30" \
      --jq "[.workflow_runs[] | select(.name == \"${workflow}\")] | sort_by(.created_at) | last | \"\\(.id)\\t\\(.status)\"" \
      2>/dev/null || true)"
    case "${found}" in
      *$'\t'completed) break ;;
    esac
    if [ "$(date +%s)" -ge "${deadline}" ]; then
      rows="${rows}"$'\n'"0"$'\t'"${workflow}"$'\t'"timed_out"
      found=""
      break
    fi
    sleep 20
  done
  if [ -n "${found}" ]; then
    rows="${rows}"$'\n'"$(gh api "repos/${repo}/actions/runs/${found%%$'\t'*}/jobs?per_page=100" \
      --jq '.jobs[] | "\(.id)\t\(.name)\t\(.conclusion // "pending")"')"
  fi
done

failed="$(printf '%s\n' "${rows}" | awk -F'\t' '$3 != "success" && $3 != "skipped" && $3 != ""')"
table="$(printf '%s\n' "${rows}" | awk -F'\t' 'NF == 3 {
  mark = ($3 == "success") ? "✓" : ($3 == "skipped") ? "–" : "✗"
  printf "| %s | %s %s |\n", $2, mark, $3 }')"

if [ -z "${failed}" ]; then
  conclusion=success
  title="Ready to merge"
  summary="Every gate passed."
else
  # The logs of what failed, read only to be matched against the catalog.
  logs="$(printf '%s\n' "${failed}" | while IFS=$'\t' read -r id _name _result; do
    [ "${id}" != 0 ] && gh api "repos/${repo}/actions/jobs/${id}/logs" 2>/dev/null || true
  done)"
  diagnosis="$(printf '%s' "${logs}" | python3 scripts/diagnose.py)"
  names="$(printf '%s\n' "${failed}" | cut -f2 | paste -sd',' - | sed 's/,/, /g')"
  conclusion=failure
  title="Held: $(printf '%s' "${diagnosis}" | jq -r .title)"
  summary="Not passing: ${names}. $(printf '%s' "${diagnosis}" | jq -r .fix)"
fi

text="$(printf '%s\n\n| Gate | Result |\n| --- | --- |\n%s\n' "${summary}" "${table}")"
jq -n \
  --arg name "${check_name}" --arg sha "${HEAD_SHA}" --arg url "${RUN_URL}" \
  --arg conclusion "${conclusion}" --arg title "${title}" --arg summary "${summary}" --arg text "${text}" \
  '{name: $name, head_sha: $sha, details_url: $url, status: "completed", conclusion: $conclusion,
    output: {title: $title, summary: $summary, text: $text}}' \
  | GH_TOKEN="${APP_TOKEN}" gh api -X POST "repos/${repo}/check-runs" --input - >/dev/null

echo "${title}"
[ "${conclusion}" = success ]
