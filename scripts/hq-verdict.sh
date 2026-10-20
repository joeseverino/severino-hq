#!/usr/bin/env bash
# HQ's verdict on a run of CI, from the results of the jobs it needs: ready,
# or held, why, and what fixes it. Run by CI's Ready job on a pull request and
# by its Release job on main, once every job they need has finished.
#
# Without APP_TOKEN it prints the verdict and exits non-zero when held. With
# it, it posts the verdict as HQ's review on the pull request: the logs of
# whatever failed are read and matched by scripts/diagnose.py, and only job
# names and the catalog's own words are posted, never a line of a log, so
# nothing private reaches this public repository.
#
# Environment:
#   RESULTS       the needs context, as JSON
#   REQUIRED      ids of needed jobs that must pass, not be skipped
#   APP_TOKEN     HQ's app token (checks: write), to post the review
#   GH_TOKEN      this run's token (actions: read), to read its jobs and logs
#   HEAD_SHA      the pull request's head commit
#   THIS_RUN      this run's id
#   RUN_URL       this run's page
set -euo pipefail

readonly check_name="Severino HQ · Review"

# A needed job that failed, was cancelled or timed out; skipped passes unless
# the job is required.
failed="$(jq -r --arg required "${REQUIRED:-}" '
  ($required | split(" ") | map(select(. != ""))) as $must
  | (to_entries[]
     | select(.value.result != "success" and (.value.result != "skipped" or (.key | IN($must[]))))
     | "\(.key): \(.value.result)"),
    (($must - keys)[] | "\(.): not run")' <<<"${RESULTS:?}")"

if [ -z "${APP_TOKEN:-}" ]; then
  if [ -n "${failed}" ]; then
    printf '%s\n' "${failed}"
    echo "::error::Not ready: every job above must pass."
    exit 1
  fi
  echo "Every gate passed."
  exit 0
fi

readonly repo="${GITHUB_REPOSITORY:?}"
# One line per job of this run: id, name, result, seconds, page. The gate's
# own job and the push-only proof are not gates of a pull request.
rows="$(gh api --paginate "repos/${repo}/actions/runs/${THIS_RUN:?}/jobs?per_page=100" \
  --jq '.jobs[] | select(.name != "Ready" and .name != "Release" and .name != "Proven on its pull request")
    | "\(.id)\t\(.name)\t\(.conclusion // "pending")\t\(if .started_at and .completed_at then ((.completed_at | fromdate) - (.started_at | fromdate)) else "" end)\t\(.html_url)"')"

# In the order the pipeline runs. A gate links to its job and says how long it took.
table="$(printf '%s\n' "${rows}" | awk -F'\t' 'NF >= 3 {
  rank = ($2 ~ /^Checks/) ? 1 : ($2 ~ /^Tests/) ? 2 : ($2 ~ /^Browser/) ? 3 : ($2 ~ /^Controller/) ? 4 \
       : ($2 ~ /^CodeQL/) ? 5 : ($2 ~ /^Image checks/) ? 7 : ($2 ~ /^Image/) ? 6 : ($2 ~ /^Composition/) ? 8 : 9
  mark = ($3 == "success") ? "✓ Passed" : ($3 == "skipped") ? "– Skipped" : ($3 == "failure") ? "✗ Failed" \
       : ($3 == "cancelled") ? "✗ Cancelled" : ($3 == "timed_out") ? "✗ Timed out" : "✗ " $3
  took = ($4 == "") ? "" : ($4 >= 60 ? int($4/60) "m " $4%60 "s" : $4 "s")
  name = ($5 == "") ? $2 : "[" $2 "](" $5 ")"
  printf "%d\t%s\t| %s | %s | %s |\n", rank, $2, name, mark, took }' \
  | sort -t$'\t' -k1,1n -k2,2 | cut -f3-)"

if [ -z "${failed}" ]; then
  conclusion=success
  title="Ready to merge"
  summary="Every gate passed."
else
  broken="$(printf '%s\n' "${rows}" | awk -F'\t' '$3 != "success" && $3 != "skipped"')"
  # The logs of what failed, read only to be matched against the catalog.
  # A log that cannot be read is reported by the API's status line alone.
  logs="$(printf '%s\n' "${broken}" | while IFS=$'\t' read -r id name _result; do
    [ -n "${id}" ] || continue
    if ! scripts/job-log.sh "${id}" 2>"${RUNNER_TEMP:-/tmp}/log-error"; then
      echo "::warning title=Log not read::${name}: $(head -n 1 "${RUNNER_TEMP:-/tmp}/log-error")" >&2
    fi
  done)"
  diagnosis="$(printf '%s' "${logs}" | python3 scripts/diagnose.py)"
  # The failed jobs by the names GitHub shows; a required job that was
  # skipped has no failed row of its own, so it is named as it was needed.
  names="$(printf '%s\n' "${broken}" | cut -f2 | sed '/^$/d')"
  [ -n "${names}" ] || names="$(printf '%s\n' "${failed}" | cut -d: -f1)"
  conclusion=failure
  title="Held: $(printf '%s' "${diagnosis}" | jq -r .title)"
  summary="Not passing: $(printf '%s\n' "${names}" | paste -sd',' - | sed 's/,/, /g'). $(printf '%s' "${diagnosis}" | jq -r .fix)"
fi

CHECK_TEXT="$(printf '| Gate | Result | Took |\n| --- | --- | ---: |\n%s\n' "${table}")" \
CHECK_URL="${RUN_URL:?}" GH_TOKEN="${APP_TOKEN}" \
  scripts/hq-check.sh "${check_name}" "${HEAD_SHA:?}" completed "${title}" "${summary}" "${conclusion}"

echo "${title}"
[ "${conclusion}" = success ]
