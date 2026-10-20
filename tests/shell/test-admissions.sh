#!/usr/bin/env bash
# Prove Compose reads each extension's own admission, one run per repository,
# when several extensions share a repository, and never names one in its log.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# run list: the run named by TEST_<status> for the repository. run download:
# an artifact exists when its name is in TEST_ARTIFACTS. api: a run is the
# shared repository's, described by TEST_STARTED. Every call is logged.
cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >>"$TEST_CALLS"
if [ "$1" = api ]; then
  case "$2" in
    repos/example/extensions/actions/runs/*) printf '%s\n' "$TEST_STARTED"; exit 0 ;;
    *) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
  esac
fi
repository="" status="" name="" dir=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo) repository=$2; shift ;;
    --status) status=$2; shift ;;
    --name) name=$2; shift ;;
    --dir) dir=$2; shift ;;
  esac
  shift
done
case "$status" in
  success) [ "$repository" = example/extensions ] && echo "$TEST_SHARED_SUCCESS" || echo 300 ;;
  completed) [ "$repository" = example/extensions ] && echo "$TEST_SHARED_NEWEST" || echo 300 ;;
  "")
    case " $TEST_ARTIFACTS " in
      *" $name "*) mkdir -p "$dir" && echo "$name" >"$dir/admission.json" ;;
      *) echo "no artifact matches $name in $repository" >&2; exit 1 ;;
    esac ;;
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
[ "$(grep -c 'run list' "$work/calls")" -eq 4 ] || fail "a repository's runs were listed once per extension, not once"
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
quiet "no successful admission"

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
