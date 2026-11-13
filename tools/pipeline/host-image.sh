#!/usr/bin/env bash
# The host image a composition is built onto, and the automation worker's
# image CI built beside it, each by digest.
#   tools/pipeline/host-image.sh REPOSITORY REF [GIVEN GIVEN_RUNNER]
# GIVEN and GIVEN_RUNNER are the images CI handed over by digest in its own
# run, used as they are; blank, each is looked up. The host image is
# ghcr.io/REPOSITORY:sha-<REF>, which an admission's dispatch may reach before
# CI has published it, so it is waited for, and a CI whose Build job failed
# ends the wait naming its failed step. The worker's is
# ghcr.io/REPOSITORY/runner under the same tag, which CI publishes first, so
# it is there once the host image is.
# It polls CI's Build job rather than `gh run watch` the run: the image is
# ready minutes before the run (CodeQL, the tests) ends, and gh watches only runs.
#
# Writes image, runner and sha to $GITHUB_OUTPUT. Needs GH_TOKEN able to read actions.
set -euo pipefail

[ "$#" -ge 2 ] && [ "$#" -le 4 ] || { echo "usage: $0 REPOSITORY REF [GIVEN GIVEN_RUNNER]" >&2; exit 2; }
repository=$1 ref=$2 given=${3:-} given_runner=${4:-}
# 240 polls 5 seconds apart, the Build job asked after every sixth: 20
# minutes is well past a host build and well short of the job's timeout.
polls=${HOST_IMAGE_POLLS:-240}

if ! [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
  echo "::error title=Invalid host ref::The host ref is not a full commit SHA."
  exit 1
fi

found() {
  {
    echo "image=$1"
    echo "runner=$2"
    echo "sha=$ref"
  } >>"$GITHUB_OUTPUT"
  echo "host image: $1"
  echo "worker image: $2"
}

if [ -n "$given" ]; then
  if [[ ! "$given" =~ ^ghcr\.io/${repository}@sha256:[0-9a-f]{64}$ ]] ||
     [[ ! "$given_runner" =~ ^ghcr\.io/${repository}/runner@sha256:[0-9a-f]{64}$ ]]; then
    echo "::error title=Invalid host image::The host or worker image is not this repository's, by digest."
    exit 1
  fi
  found "$given" "$given_runner"
  exit 0
fi

# The Build job's conclusion, empty while it runs or before CI has started.
image_job() {
  local run
  run=$(gh api "repos/$repository/actions/runs?head_sha=$ref&per_page=30" \
    --jq '[.workflow_runs[] | select(.name == "CI") | .id] | first // empty' 2>/dev/null || true)
  [ -n "$run" ] || return 0
  gh api --paginate "repos/$repository/actions/runs/$run/jobs?per_page=100" \
    --jq '.jobs[] | select(.name == "Build") | (.conclusion // "-") + "\t" + ([.steps[] | select(.conclusion == "failure") | .name] | first // "")' \
    2>/dev/null | head -n 1 || true
}

image="ghcr.io/$repository:sha-${ref:0:12}"
for attempt in $(seq 1 "$polls"); do
  if digest=$(docker buildx imagetools inspect "$image" --format '{{.Manifest.Digest}}' 2>/dev/null) &&
     [ -n "$digest" ]; then
    runner="ghcr.io/$repository/runner:sha-${ref:0:12}"
    if ! runner_digest=$(docker buildx imagetools inspect "$runner" --format '{{.Manifest.Digest}}' 2>/dev/null) ||
       [ -z "$runner_digest" ]; then
      echo "::error title=No worker image::CI published the host image for ${ref:0:12} without $runner, which it publishes first."
      exit 1
    fi
    found "${image%:*}@$digest" "${runner%:*}@$runner_digest"
    exit 0
  fi
  [ "$attempt" -eq 1 ] && echo "waiting for the host image $image to be published..."
  # Every 30 seconds, whether there is still a build to wait for: a host
  # build that failed will never publish.
  if [ $((attempt % 6)) -eq 0 ]; then
    IFS=$'\t' read -r conclusion step <<<"$(image_job)" || true
    case "${conclusion:-}" in
      failure | cancelled | timed_out | skipped)
        echo "::error title=Host image $conclusion${step:+ at $step}::CI's Build job for ${ref:0:12} concluded '$conclusion'${step:+ in step \"$step\"}, so there is no host image to compose onto. Fix that, and this composition runs with it."
        {
          echo "### No host image to compose onto"
          echo
          echo "CI's **Build** job for \`${ref:0:12}\` ${conclusion}${step:+ at **${step}**}."
        } >>"$GITHUB_STEP_SUMMARY"
        exit 1
        ;;
    esac
  fi
  sleep 5
done
echo "::error title=Host image never appeared::$image was not published within 20 minutes. The host build for this commit failed or was skipped; the composition has nothing to compose onto."
exit 1
