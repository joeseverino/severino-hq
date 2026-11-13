#!/usr/bin/env bash
# Prove Compose takes the host and worker images CI handed over, waits for a
# host image CI is still building, finds the worker's beside it, and stops
# waiting, naming the failed step, once CI's Build job failed.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# docker buildx imagetools inspect: the digest once it has been asked
# TEST_PUBLISHED_AFTER times (never when unset). Each ask is counted, the
# worker image's apart, which is there unless TEST_NO_RUNNER is set.
cat >"$bin/docker" <<'STUB'
#!/usr/bin/env bash
case "$*" in
  */runner:*)
    echo "runner tag=${4##*:}" >>"$TEST_CALLS"
    [ -z "$TEST_NO_RUNNER" ] || { echo "not found" >&2; exit 1; }
    echo "sha256:$(printf 'e%.0s' $(seq 64))"
    exit 0
    ;;
esac
echo inspect >>"$TEST_CALLS"
asked=$(grep -c inspect "$TEST_CALLS")
[ -n "$TEST_PUBLISHED_AFTER" ] && [ "$asked" -gt "$TEST_PUBLISHED_AFTER" ] || { echo "not found" >&2; exit 1; }
echo "sha256:$(printf 'd%.0s' $(seq 64))"
STUB
# gh api: CI's run for the commit, and its Build job as TEST_IMAGE_JOB describes it.
cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "gh $*" >>"$TEST_CALLS"
case "$*" in
  *"/runs?head_sha="*) echo 42 ;;
  *"/runs/42/jobs"*) printf '%s\n' "$TEST_IMAGE_JOB" ;;
  *) exit 1 ;;
esac
STUB
cat >"$bin/sleep" <<'STUB'
#!/bin/sh
echo sleep >>"$TEST_CALLS"
STUB
chmod +x "$bin/docker" "$bin/gh" "$bin/sleep"

ref=$(printf 'a%.0s' $(seq 40))
digest="sha256:$(printf 'd%.0s' $(seq 64))"
runner="ghcr.io/example/host/runner@sha256:$(printf 'e%.0s' $(seq 64))"
host_image() {
  : >"$work/calls"
  : >"$work/github-output"
  : >"$work/summary"
  PATH="$bin:$PATH" TEST_CALLS="$work/calls" GITHUB_OUTPUT="$work/github-output" GITHUB_STEP_SUMMARY="$work/summary" \
    TEST_PUBLISHED_AFTER="${PUBLISHED_AFTER-}" TEST_NO_RUNNER="${NO_RUNNER-}" TEST_IMAGE_JOB="${IMAGE_JOB-}" HOST_IMAGE_POLLS="${POLLS-240}" \
    "$repo_dir/tools/pipeline/host-image.sh" example/host "$@" >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" "$work/calls" >&2; exit 1; }
output() { sed -n "s/^$1=//p" "$work/github-output"; }

host_image "$ref" "ghcr.io/example/host@$digest" "$runner" || fail "the images CI handed over were refused"
[ "$(output image)" = "ghcr.io/example/host@$digest" ] && [ "$(output runner)" = "$runner" ] && [ "$(output sha)" = "$ref" ] ||
  fail "the images CI handed over were not the ones composed"
[ ! -s "$work/calls" ] || fail "an image CI handed over was looked up"
if host_image "$ref" "ghcr.io/example/other@$digest" "$runner"; then fail "another repository's image was composed"; fi
if host_image "$ref" "ghcr.io/example/host:latest" "$runner"; then fail "an image not named by digest was composed"; fi
if host_image "$ref" "ghcr.io/example/host@$digest" "ghcr.io/example/other/runner@${runner#*@}"; then
  fail "another repository's worker image was composed"
fi
if host_image "$ref" "ghcr.io/example/host@$digest" "ghcr.io/example/host/runner:latest"; then
  fail "a worker image not named by digest was composed"
fi
if host_image "$ref" "ghcr.io/example/host@$digest"; then fail "a host image without its worker's was composed"; fi
if host_image "${ref:0:12}"; then fail "a short host ref was accepted"; fi

PUBLISHED_AFTER=0 host_image "$ref" "" "" || fail "a published image was not found"
[ "$(output image)" = "ghcr.io/example/host@$digest" ] || fail "a published image was not composed by digest"
[ "$(output runner)" = "$runner" ] || fail "the worker image published beside it was not composed by digest"
grep -q "runner tag=sha-${ref:0:12}" "$work/calls" || fail "the worker image was not looked up by the commit's tag"
! grep -q -e gh -e sleep "$work/calls" || fail "a published image was waited for"

PUBLISHED_AFTER=0 NO_RUNNER=1 host_image "$ref" && fail "a host image published without its worker's was composed"
grep -q '::error title=No worker image::' "$work/out" || fail "a missing worker image was not named"

PUBLISHED_AFTER=13 IMAGE_JOB="-	" host_image "$ref" || fail "an image CI was still building was not waited for"
[ "$(output image)" = "ghcr.io/example/host@$digest" ] || fail "the image CI published was not composed"
grep -q 'runs/42/jobs' "$work/calls" || fail "CI's Build job was not asked about while waiting"

PUBLISHED_AFTER="" IMAGE_JOB="$(printf 'failure\tBuild the image')" host_image "$ref" &&
  fail "a composition waited on a Build job that failed"
grep -q '::error title=Host image failure at Build the image::' "$work/out" || fail "the failed Build step was not named"
grep -q 'Build the image' "$work/summary" || fail "the summary does not name the failed Build step"
[ "$(grep -c inspect "$work/calls")" -le 6 ] || fail "the wait went on after the Build job failed"
[ ! -s "$work/github-output" ] || fail "a failed Build job left a host image"

PUBLISHED_AFTER="" IMAGE_JOB="-	" POLLS=12 host_image "$ref" && fail "an image that never appeared was composed"
grep -q '::error title=Host image never appeared::' "$work/out" || fail "a wait that ran out did not say so"
[ "$(grep -c inspect "$work/calls")" -eq 12 ] || fail "the wait did not end at its bound"
echo "host image tests passed"
