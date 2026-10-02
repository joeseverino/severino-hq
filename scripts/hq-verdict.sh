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
# "Proven on its pull request" runs on pushes to main only, so a pull request
# never has it.
# One line per job: id, name, result, seconds, page, workflow.
readonly job_line='"\(.id)\t\(.name)\t\(.conclusion // "pending")\t\(if .started_at and .completed_at then ((.completed_at | fromdate) - (.started_at | fromdate)) else "" end)\t\(.html_url)"'
rows="$(gh api "repos/${repo}/actions/runs/${THIS_RUN}/jobs?per_page=100" \
  --jq ".jobs[] | select(.name != \"Ready\" and .name != \"Proven on its pull request\") | ${job_line} + \"\\tCI\"")"

# The same commit's other workflows, once each has finished: Compose, which builds
# HQ from it (only when the pull request is composed), and CodeQL.
others="CodeQL"
[ "${COMPOSED:-false}" = true ] && others="Compose CodeQL"
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
      rows="${rows}"$'\n'"0"$'\t'"${workflow}"$'\t'"timed_out"$'\t\t\t'"${workflow}"
      found=""
      break
    fi
    sleep 20
  done
  if [ -n "${found}" ]; then
    rows="${rows}"$'\n'"$(gh api "repos/${repo}/actions/runs/${found%%$'\t'*}/jobs?per_page=100" \
      --jq ".jobs[] | ${job_line} + \"\\t${workflow}\"")"
  fi
done

failed="$(printf '%s\n' "${rows}" | awk -F'\t' '$3 != "success" && $3 != "skipped" && $3 != ""')"
# In the order the pipeline runs: CI's Checks, Tests, Browser and Image, then
# Compose, then CodeQL. A gate links to its job and says how long it took.
table="$(printf '%s\n' "${rows}" | awk -F'\t' 'NF >= 3 {
  order = ($6 == "CI") ? 1 : ($6 == "Compose") ? 2 : 3
  rank = ($2 ~ /^Checks/) ? 1 : ($2 ~ /^Tests/) ? 2 : ($2 ~ /^Browser/) ? 3 : ($2 ~ /^Image/) ? 4 : 5
  mark = ($3 == "success") ? "✓ Passed" : ($3 == "skipped") ? "– Skipped" : ($3 == "failure") ? "✗ Failed" \
       : ($3 == "cancelled") ? "✗ Cancelled" : ($3 == "timed_out") ? "✗ Timed out" : "✗ " $3
  took = ($4 == "") ? "" : ($4 >= 60 ? int($4/60) "m " $4%60 "s" : $4 "s")
  name = ($5 == "") ? $2 : "[" $2 "](" $5 ")"
  printf "%d\t%d\t%s\t| %s | %s | %s | %s |\n", order, rank, $2, $6, name, mark, took }' \
  | sort -t$'\t' -k1,1n -k2,2n -k3,3 | cut -f4-)"

if [ -z "${failed}" ]; then
  conclusion=success
  title="Ready to merge"
  summary="Every gate passed."
else
  # The logs of what failed, read only to be matched against the catalog.
  # A log that cannot be read is said, by status alone, so the diagnosis that
  # follows is not mistaken for one made from the log.
  logs="$(printf '%s\n' "${failed}" | while IFS=$'\t' read -r id name _result; do
    [ "${id}" != 0 ] || continue
    if ! scripts/job-log.sh "${id}" 2>"${RUNNER_TEMP:-/tmp}/log-error"; then
      echo "::warning title=Log not read::${name}: $(head -n 1 "${RUNNER_TEMP:-/tmp}/log-error")" >&2
    fi
  done)"
  diagnosis="$(printf '%s' "${logs}" | python3 scripts/diagnose.py)"
  names="$(printf '%s\n' "${failed}" | cut -f2 | paste -sd',' - | sed 's/,/, /g')"
  conclusion=failure
  title="Held: $(printf '%s' "${diagnosis}" | jq -r .title)"
  summary="Not passing: ${names}. $(printf '%s' "${diagnosis}" | jq -r .fix)"
fi

CHECK_TEXT="$(printf '| Workflow | Gate | Result | Took |\n| --- | --- | --- | ---: |\n%s\n' "${table}")" \
CHECK_URL="${RUN_URL}" GH_TOKEN="${APP_TOKEN}" \
  scripts/hq-check.sh "${check_name}" "${HEAD_SHA}" completed "${title}" "${summary}" "${conclusion}"

echo "${title}"
[ "${conclusion}" = success ]
