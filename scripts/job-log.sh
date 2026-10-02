#!/usr/bin/env bash
# Prints one Actions job's log, read only to be matched against the diagnosis
# catalog.
#   scripts/job-log.sh JOB_ID
# Through curl rather than gh: gh refuses a response that holds terminal escape
# sequences, and a job log is full of them. curl sends the token to the API
# only, not to the storage host the log redirects to. Needs GH_TOKEN with
# actions: read.
set -euo pipefail

job="${1:?usage: job-log.sh JOB_ID}"
[[ "$job" =~ ^[0-9]+$ ]] || { echo "job-log.sh: not a job id: $job" >&2; exit 2; }
curl -fsSL --retry 2 \
  -H "Authorization: Bearer ${GH_TOKEN:?}" \
  -H "Accept: application/vnd.github+json" \
  "${GITHUB_API_URL:-https://api.github.com}/repos/${GITHUB_REPOSITORY:?}/actions/jobs/${job}/logs"
