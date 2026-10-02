#!/usr/bin/env bash
# Names the steps that failed in one job of the current run.
#
#   scripts/failed-step.sh "JOB NAME"
#
# Prints one step name per line, or nothing when none failed. Needs GH_TOKEN
# with actions: read. The job name is the workflow's own, never user input.
set -euo pipefail
job="${1:?usage: failed-step.sh JOB_NAME}"
gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${GITHUB_RUN_ID}/attempts/${GITHUB_RUN_ATTEMPT:-1}/jobs?per_page=100" \
  --jq ".jobs[] | select(.name == \"${job//\"/}\") | .steps[] | select(.conclusion == \"failure\") | .name"
