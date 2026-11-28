#!/usr/bin/env bash
# Prove Compose finds every extension repository carrying the pull request's
# branch, and the one repository with a pull request, open or merged, whose
# head is a commit; that it fails when it cannot tell, and never names a
# repository in its log.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# gh api PATH --jq QUERY answers GitHub's body for PATH through QUERY, as gh
# does, and a 502 for any repository in TEST_BROKEN.
# repos/<repository>/branches/<branch>: the branch when the repository is in
# TEST_CARRYING, a 404 when not.
# repos/<repository>/commits/<sha>/pulls: the pull requests with that head,
# in GitHub's shape: a merged one where the repository is in TEST_MERGED
# (based on d...), then an open one where it is in TEST_OPEN (based on b...),
# then one whose head is another commit where it is in TEST_HOLDING; a 422
# where it is in none, since it does not hold the commit.
cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
repository=${2#repos/}
repository=${repository%%/branches/*}
repository=${repository%%/commits/*}
listed() { case " $1 " in *" $repository "*) return 0 ;; esac; return 1; }
listed "$TEST_BROKEN" && { echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1; }
pull() { printf '{"number":%s,"state":"%s","merged_at":%s,"head":{"sha":"%s"},"base":{"sha":"%s"}}' "$@"; }
head=${2#*/commits/}
head=${head%/pulls}
case "$2" in
  */pulls)
    listed "$TEST_MERGED $TEST_OPEN $TEST_HOLDING" || { echo "gh: No commit found for SHA (HTTP 422)" >&2; exit 1; }
    pulls=()
    listed "$TEST_MERGED" && pulls+=("$(pull 1 closed '"2030-01-01T00:00:00Z"' "$head" "$(printf 'd%.0s' $(seq 40))")")
    listed "$TEST_OPEN" && pulls+=("$(pull 2 open null "$head" "$(printf 'b%.0s' $(seq 40))")")
    listed "$TEST_HOLDING" && pulls+=("$(pull 3 open null "$(printf 'f%.0s' $(seq 40))" "$(printf 'b%.0s' $(seq 40))")")
    body="[$(IFS=,; printf '%s' "${pulls[*]}")]" ;;
  *)
    listed "$TEST_CARRYING" || { echo "gh: Not Found (HTTP 404)" >&2; exit 1; }
    body="{\"commit\":{\"sha\":\"$(printf '%s' "$repository" | tr -c 'a-f0-9' 'a' | cut -c1-8)\"}}" ;;
esac
printf '%s' "$body" | jq -r "$4"
STUB
chmod +x "$bin/gh"

printf '%s' '{"extensions": [
  {"id": "example.alpha", "repository": "example/extensions"},
  {"id": "example.beta", "repository": "example/extensions"},
  {"id": "example.gamma", "repository": "example/gamma"}]}' >"$work/set.json"

coordinated() {
  local branch=$1
  : >"$work/github-output"
  (cd "$work" && PATH="$bin:$PATH" GITHUB_OUTPUT="$work/github-output" \
    TEST_CARRYING="${CARRYING-}" TEST_BROKEN="${BROKEN-}" \
    "$repo_dir/tools/pipeline/coordinated-branches.sh" set.json "$branch" coordinated.txt) >"$work/out" 2>&1
}
fail() { echo "$1" >&2; cat "$work/out" >&2; exit 1; }
quiet() {
  for name in example/extensions example/gamma example.alpha; do
    if grep -q "$name" "$work/out"; then fail "$1: the public log names $name"; fi
  done
}

CARRYING="example/gamma" coordinated feature/both || fail "a carrying repository did not resolve"
[ "$(cut -d= -f1 "$work/coordinated.txt")" = example/gamma ] || fail "the carrying repository was not listed alone"
grep -qx 'found=true' "$work/github-output" || fail "a coordinated branch was not reported as found"
quiet "a coordinated branch"

CARRYING="example/extensions example/gamma" coordinated feature/both || fail "two carrying repositories did not resolve"
[ "$(grep -c . "$work/coordinated.txt")" -eq 2 ] || fail "a repository holding two extensions was listed twice"

coordinated feature/host-only || fail "a branch only the host carries did not resolve"
[ ! -s "$work/coordinated.txt" ] || fail "a branch no extension carries listed one"
grep -qx 'found=false' "$work/github-output" || fail "no coordinated branch was not reported as none"

if BROKEN="example/gamma" coordinated feature/both; then fail "an unanswered question was taken as no branch"; fi
grep -q '::error::' "$work/out" || fail "an unanswered question did not fail with an error"
quiet "an unanswered question"

if coordinated 'feature;id'; then fail "a branch name with shell characters was accepted"; fi

head=$(printf 'c%.0s' $(seq 40))
pulled() {
  : >"$work/github-output"
  (cd "$work" && PATH="$bin:$PATH" GITHUB_OUTPUT="$work/github-output" \
    TEST_OPEN="${OPEN-}" TEST_MERGED="${MERGED-}" TEST_HOLDING="${HOLDING-}" TEST_BROKEN="${BROKEN-}" \
    "$repo_dir/tools/pipeline/coordinated-branches.sh" set.json --commit "${1-$head}" coordinated.txt) >"$work/out" 2>&1
}
OPEN="example/gamma" HOLDING="example/extensions" pulled || fail "an extension pull request's head did not resolve"
[ "$(cat "$work/coordinated.txt")" = "example/gamma=$head" ] || fail "the pull request's repository was not listed at its head"
grep -qx "base=$(printf 'b%.0s' $(seq 40))" "$work/github-output" || fail "the pull request's base was not reported"
grep -qx 'name=gamma' "$work/github-output" || fail "the pull request's repository name was not reported"
grep -qx 'found=true' "$work/github-output" || fail "an extension pull request was not reported as found"
quiet "an extension pull request"
MERGED="example/gamma" HOLDING="example/extensions" pulled || fail "a pull request that merged while its check ran did not resolve"
[ "$(cat "$work/coordinated.txt")" = "example/gamma=$head" ] || fail "the merged pull request's repository was not listed at its head"
grep -qx "base=$(printf 'd%.0s' $(seq 40))" "$work/github-output" || fail "the merged pull request's base was not reported"
MERGED="example/gamma" OPEN="example/gamma" pulled || fail "a head both an open and a merged pull request have did not resolve"
grep -qx "base=$(printf 'b%.0s' $(seq 40))" "$work/github-output" || fail "the open pull request's base was not taken first"
if HOLDING="example/gamma" pulled; then fail "a commit no pull request has as its head was checked"; fi
grep -q '::error title=No extension pull request::' "$work/out" || fail "a commit with no pull request did not fail with an error"
if OPEN="example/gamma" BROKEN="example/extensions" pulled; then fail "an unanswered question was taken as no pull request"; fi
if OPEN="example/gamma" pulled ffff; then fail "a commit that is not a full SHA was accepted"; fi
echo "coordinated branch tests passed"
