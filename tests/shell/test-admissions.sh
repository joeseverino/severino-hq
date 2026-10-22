#!/usr/bin/env bash
# Prove Compose reads each extension's own admission, one run per repository,
# when several extensions share a repository, and never names one in its log.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# api .../workflows/<file>/runs: the shared repository's newest runs, newest
# first: a success on a branch and one from a pull request, which never count,
# then on main TEST_SHARED_NEWEST failed unless it is TEST_SHARED_SUCCESS, and
# TEST_SHARED_SUCCESS; none on main when TEST_NO_MAIN is set, and a failed call
# when TEST_API_FAILS is. Any other repository's newest is 300, a success. The
# --jq filter runs in jq. run download: an artifact exists when its name is in
# TEST_ARTIFACTS. api .../runs/<id>: a run is the shared repository's,
# described by TEST_STARTED. Every call is logged.
cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"$TEST_CALLS"
if [ "$1" = api ]; then
  case "$2" in
    repos/example/extensions/actions/workflows/*/runs\?*)
      if [ -n "$TEST_API_FAILS" ]; then
        echo "HTTP 502: Bad Gateway (https://api.github.com/$2)" >&2
        exit 1
      fi
      [ "$3" = --jq ] || exit 2
      jq -n --arg success "$TEST_SHARED_SUCCESS" --arg newest "$TEST_SHARED_NEWEST" --arg none "$TEST_NO_MAIN" '
        def run($id; $branch; $event; $conclusion):
          {id: $id, head_branch: $branch, event: $event, status: "completed", conclusion: $conclusion};
        {workflow_runs: (
          [run(999; "feature"; "push"; "success"), run(998; "main"; "pull_request"; "success")] +
          if $none != "" then [] else
            (if $newest != "" and $newest != $success then [run($newest | tonumber; "main"; "push"; "failure")] else [] end) +
            (if $success != "" then [run($success | tonumber; "main"; "push"; "success")] else [] end)
          end)}' | jq -r "$4"
      exit ;;
    repos/*/actions/workflows/*/runs\?*)
      [ "$3" = --jq ] || exit 2
      jq -n '{workflow_runs: [{id: 300, head_branch: "main", event: "push", status: "completed", conclusion: "success"}]}' | jq -r "$4"
      exit ;;
    repos/example/extensions/actions/runs/*) printf '%s\n' "$TEST_STARTED"; exit 0 ;;
    *) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  esac
fi
[ "$1 $2" = "run download" ] || exit 2
repository="" name="" dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) repository=$2; shift ;;
    --name) name=$2; shift ;;
    --dir) dir=$2; shift ;;
  esac
  shift
done
case " $TEST_ARTIFACTS " in
  *" $name "*) mkdir -p "$dir" && echo "$name" >"$dir/admission.json" ;;
  *) echo "no artifact matches $name in $repository" >&2; exit 1 ;;
esac
STUB
chmod +x "$bin/gh"

workflow=.github/workflows/admit-plugin.yml
two_in_one="{\"extensions\": [
  {\"id\": \"example.beta\", \"repository\": \"example/extensions\", \"workflow\": \"$workflow\"},
  {\"id\": \"example.alpha\", \"repository\": \"example/extensions\", \"workflow\": \"$workflow\"},
  {\"id\": \"example.gamma\", \"repository\": \"example/gamma\", \"workflow\": \"$workflow\"}]}"
every_artifact="admission-example.alpha admission-example.beta admission-example.gamma"

admissions() {
  local set=$1; shift
  printf '%s' "$set" >"$work/set.json"
  : >"$work/calls"
  rm -rf "$work/withheld" "$work/artifacts"
  (cd "$work" && PATH="$bin:$PATH" TEST_CALLS="$work/calls" WITHHELD_DIR="$work/withheld" \
    TEST_SHARED_SUCCESS="${SUCCESS-200}" TEST_SHARED_NEWEST="${NEWEST-200}" \
    TEST_NO_MAIN="${NO_MAIN-}" TEST_API_FAILS="${API_FAILS-}" \
    TEST_ARTIFACTS="${ARTIFACTS-$every_artifact}" ADMISSION_RUN="${STARTED_RUN-}" \
    TEST_STARTED="${STARTED-$(printf '%s\t%s\t%s\t%s' "$workflow" main push -)}" \
    "$repo_dir/tools/pipeline/admissions.sh" find set.json admissions.txt &&
    PATH="$bin:$PATH" TEST_CALLS="$work/calls" WITHHELD_DIR="$work/withheld" \
    TEST_ARTIFACTS="${ARTIFACTS-$every_artifact}" \
    "$repo_dir/tools/pipeline/admissions.sh" fetch admissions.txt artifacts) >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }
quiet() {
  for name in example.alpha example.beta example.gamma example/extensions; do
    if grep -q "$name" "$work/out"; then fail "$1: the public log names $name"; fi
  done
}

admissions "$two_in_one" || fail "two extensions in one repository did not resolve"
[ "$(cat "$work/admissions.txt")" = "$(printf '%s\t%s\t%s\n' \
  example.alpha example/extensions 200 example.beta example/extensions 200 example.gamma example/gamma 300)" ] ||
  fail "each extension is not paired with its repository's newest successful run"
[ "$(grep -c '/runs?' "$work/calls")" -eq 2 ] || fail "a repository's runs were listed once per extension, not once"
for id in example.alpha example.beta example.gamma; do
  [ "$(cat "$work/artifacts/$id/admission.json")" = "admission-$id" ] || fail "$id did not get its own artifact"
done
quiet "a composition"

fingerprint() { sha256sum "$work/admissions.txt" | cut -d' ' -f1; }
before=$(fingerprint)
SUCCESS=201 NEWEST=201 admissions "$two_in_one" || fail "a newer admission did not resolve"
[ "$(fingerprint)" != "$before" ] || fail "a new admission of one repository left the fingerprint as it was"
admissions "$(jq '.extensions |= map(select(.id != "example.beta"))' <<<"$two_in_one")" || fail "a smaller set did not resolve"
[ "$(fingerprint)" != "$before" ] || fail "dropping one of a repository's extensions left the fingerprint as it was"

NEWEST=201 admissions "$two_in_one" || fail "a failed newer admission held the composition back"
grep -q '::warning::' "$work/out" || fail "a failed newer admission was not warned about"
grep -q "example.alpha	example/extensions	200" "$work/admissions.txt" || fail "a failed admission was composed"
quiet "a failed newer admission"

if ARTIFACTS="admission-example.alpha admission-example.gamma" admissions "$two_in_one"; then
  fail "a listed extension with no artifact in its run was composed"
fi
grep -q '::error::' "$work/out" || fail "a missing artifact did not fail with an error"
quiet "a missing artifact"
grep -q 'example.beta' "$work/withheld/admissions.log" || fail "the withheld log does not say which artifact is missing"

if SUCCESS="" admissions "$two_in_one"; then fail "a repository with no successful admission was composed"; fi
grep -q 'no successful admission on main' "$work/out" || fail "a repository with no successful admission did not say so"
quiet "no successful admission"

if NO_MAIN=1 admissions "$two_in_one"; then fail "a repository with no run on main in the page was composed"; fi
grep -q 'no admission run on main among its newest 100' "$work/out" ||
  fail "a page with no run on main did not say so"
! grep -q 'no successful admission' "$work/out" || fail "a page with no run on main read as no successful admission"
quiet "no run on main"

if API_FAILS=1 admissions "$two_in_one"; then fail "a failed API call was composed"; fi
grep -q '::error::the GitHub API failed .*HTTP 502: Bad Gateway' "$work/out" || fail "a failed API call did not fail with the API's error"
! grep -q 'admission on main\|admission run on main' "$work/out" || fail "a failed API call read as a missing admission"
quiet "a failed API call"
grep -q 'example/extensions' "$work/withheld/admissions.log" || fail "the withheld log does not say which repository's call failed"

if admissions "$(jq '.extensions[0].workflow = ".github/workflows/other.yml"' <<<"$two_in_one")"; then
  fail "two signers for one repository were accepted"
fi
quiet "two signers"

STARTED_RUN=250 admissions "$two_in_one" || fail "the run that started the composition was not composed"
grep -q "example.beta	example/extensions	250" "$work/admissions.txt" || fail "a finishing admission was passed over for an older one"
grep -q "example.gamma	example/gamma	300" "$work/admissions.txt" || fail "another repository took the run that started the composition"
! grep -q '::warning::' "$work/out" || fail "a finishing admission was warned about as a failed one"
quiet "the run that started the composition"

STARTED_RUN=150 admissions "$two_in_one" || fail "an older starting run did not resolve"
grep -q "example.beta	example/extensions	200" "$work/admissions.txt" || fail "an older run displaced a newer successful one"

SUCCESS="" STARTED_RUN=250 admissions "$two_in_one" || fail "a repository's first admission was not composed"

for wrong in "$(printf '%s\t%s\t%s\t%s' "$workflow" feature push -)" \
             "$(printf '%s\t%s\t%s\t%s' "$workflow" main pull_request -)" \
             "$(printf '%s\t%s\t%s\t%s' "$workflow" main push failure)" \
             "$(printf '%s\t%s\t%s\t%s' .github/workflows/other.yml main push -)"; do
  if STARTED_RUN=250 STARTED="$wrong" admissions "$two_in_one"; then
    fail "a starting run that is not a passing admission on main was composed: $wrong"
  fi
  quiet "a starting run that is not an admission"
done

admissions '{"extensions": []}' || fail "a composition of no extensions did not resolve"
[ ! -s "$work/admissions.txt" ] || fail "a composition of no extensions resolved an admission"
echo "admission resolution tests passed"
