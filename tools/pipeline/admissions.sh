#!/usr/bin/env bash
# The extensions' signed admissions: which run holds each one, and its files.
#   tools/pipeline/admissions.sh find SET OUT    # OUT: id<TAB>repository<TAB>run, one line per extension
#   tools/pipeline/admissions.sh fetch IN DIR    # each extension's own artifact into DIR/<id>
# SET is the composition set, in the shape of composition/extensions.json.
#
# Several extensions may live in one repository. One repository is one trust
# domain: its extensions are signed by the same admission workflow and admitted
# together by one run, which is resolved once per repository: the newest
# successful run on main. Each extension's artifact, `admission-<id>`, is read
# from that run (.github/actions/admit-plugin/action.yml names it).
#
# An admission starts its composition while its run is still finishing, so that
# run is not yet a successful one. ADMISSION_RUN names it: the repository it
# belongs to is composed from it when it is that repository's admission on
# main, not failed, and newer than its newest successful run. Its signatures
# are verified like any other's.
#
# OUT is the whole input to Compose's "has anything changed?": a new wheel
# cannot exist without a new run, and a listed extension is a line of its own,
# so a change to any one package or to the set changes OUT.
#
# This runs in a public log, so nothing it prints names an extension or a
# repository; what failed is written to $WITHHELD_DIR, which is sealed. Needs
# GH_TOKEN able to read the extensions' runs.
set -euo pipefail

usage() { echo "usage: $0 find SET OUT | fetch IN DIR" >&2; exit 2; }
fail() { echo "::error::$1" >&2; exit 1; }

withheld="${WITHHELD_DIR:-${TMPDIR:-/tmp}}"
mkdir -p "$withheld"
log="$withheld/admissions.log"

# Whether RUN is REPOSITORY's admission by WORKFLOW on main, finishing or
# finished well. Not that repository's run at all is "no"; anything else
# unexpected fails.
admitting() {
  local repository=$1 workflow=$2 run=$3 found path branch event conclusion
  if ! found=$(gh api "repos/$repository/actions/runs/$run" \
        --jq '[.path, .head_branch, .event, (.conclusion // "-")] | @tsv' 2>"$withheld/run.err"); then
    grep -q 'HTTP 404' "$withheld/run.err" && return 1
    cat "$withheld/run.err" >>"$log"
    fail "could not ask an extension repository for the admission run that started this composition"
  fi
  IFS=$'\t' read -r path branch event conclusion <<<"$found"
  if [ "${path%%@*}" != "$workflow" ] || [ "$branch" != main ] || [ "$event" = pull_request ] ||
     [[ "$conclusion" != - && "$conclusion" != success ]]; then
    echo "run $run of $repository: $found" >>"$log"
    fail "the admission run that started this composition is not a passing admission on main"
  fi
}

# REPOSITORY's WORKFLOW runs on main among its newest 100, as three words: how
# many there are, the newest successful one and the newest finished one, `-`
# for none. Filtered here: the API's status and branch parameters can answer an
# empty list with success for runs that exist. gh retries nothing, so a failed
# call fails here with its own error, the repository and URLs taken out.
main_runs() {
  local repository=$1 workflow=$2 error
  if gh api "repos/$repository/actions/workflows/${workflow##*/}/runs?per_page=100" --jq '
        [.workflow_runs[] | select(.head_branch == "main" and .event != "pull_request")] |
        "\(length) \(map(select(.conclusion == "success"))[0].id // "-") \(map(select(.status == "completed"))[0].id // "-")"' \
        2>"$withheld/runs.err"; then
    return
  fi
  { echo "listing the runs of $repository:"; cat "$withheld/runs.err"; } >>"$log"
  error=$(head -n 1 "$withheld/runs.err")
  error=${error//"$repository"/an extension repository}
  error=$(sed -E 's#https?://[^ )]+#<url>#g' <<<"$error")
  fail "the GitHub API failed listing an extension repository's admission runs: ${error:-gh said nothing}"
}

find_admissions() {
  local set=$1 out=$2 repository workflow listed count run newest started=${ADMISSION_RUN:-} claimed
  [[ -z "$started" || "$started" =~ ^[0-9]+$ ]] || fail "the admission run that started this composition is not a run id"
  jq -e '.extensions | type == "array"' "$set" >/dev/null 2>&1 ||
    fail "the composition set has no extensions list"
  jq -e '.extensions | group_by(.repository) | all(map(.workflow) | unique | length == 1)' "$set" >/dev/null ||
    fail "extensions that share a repository must name the same admission workflow: one repository is one signer"
  : >"$out"
  while IFS=$'\t' read -r repository workflow; do
    # A failed call stops the script here, with the API's error.
    listed=$(main_runs "$repository" "$workflow")
    read -r count run newest <<<"$listed"
    claimed=false
    if [ -n "$started" ] && admitting "$repository" "$workflow" "$started" &&
       { [[ ! "$run" =~ ^[0-9]+$ ]] || [ "$started" -gt "$run" ]; }; then
      run=$started claimed=true
    fi
    if [[ ! "$run" =~ ^[0-9]+$ ]] && [ "$count" = 0 ]; then
      echo "$repository has no admission run on main among its newest 100" >>"$log"
      fail "an extension repository has no admission run on main among its newest 100, so whether one succeeded is unknown"
    fi
    if [[ ! "$run" =~ ^[0-9]+$ ]]; then
      echo "$repository has no successful admission on main" >>"$log"
      fail "an extension repository has no successful admission on main"
    fi
    # One that failed since does not hold back the host or the other
    # extensions: the composed suite runs every extension's tests against this
    # host, so an older wheel is proven before it ships. One still running
    # starts its own composition when it ends.
    if [ "$claimed" = false ] && [ "$newest" != "$run" ]; then
      echo "::warning::an extension repository's newest admission on main failed; composing its last successful one"
    fi
    jq -r --arg repository "$repository" --arg run "$run" \
      '.extensions[] | select(.repository == $repository) | [.id, .repository, $run] | @tsv' "$set" >>"$out"
  done < <(jq -r '.extensions | unique_by(.repository)[] | [.repository, .workflow] | @tsv' "$set")
  sort -o "$out" "$out"
  printf 'resolved %s extension(s) from %s admission run(s)\n' \
    "$(grep -c . "$out" || true)" "$(cut -f3 "$out" | sort -u | grep -c . || true)"
}

fetch_admissions() {
  local in=$1 dir=$2 id repository run
  mkdir -p "$dir"
  while IFS=$'\t' read -r id repository run; do
    [ -n "$id" ] || continue
    # The id becomes a directory and an artifact name: a plain name only.
    [[ "$id" =~ ^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*$ ]] || fail "an extension id is not a plain dotted name"
    if ! gh run download "$run" --repo "$repository" --name "admission-$id" --dir "$dir/$id" 2>>"$log"; then
      echo "$id: no admission-$id in run $run of $repository, or it could not be read" >>"$log"
      fail "a listed extension has no artifact of its own in its repository's newest successful admission run; which one is in this run's sealed failure logs"
    fi
  done <"$in"
}

case "${1:-}" in
  find) [ "$#" -eq 3 ] || usage; find_admissions "$2" "$3" ;;
  fetch) [ "$#" -eq 3 ] || usage; fetch_admissions "$2" "$3" ;;
  *) usage ;;
esac
