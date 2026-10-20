#!/usr/bin/env bash
# Prove Compose finds every extension repository carrying the pull request's
# branch, fails when it cannot tell, and never names a repository in its log.
set -euo pipefail

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
bin="$work/bin"
mkdir -p "$bin"

# gh api repos/<repository>/branches/<branch>: the commit when the repository
# is in TEST_CARRYING, a 404 when not, and a 502 for any in TEST_BROKEN.
cat >"$bin/gh" <<'STUB'
#!/usr/bin/env bash
repository=${2#repos/}
repository=${repository%%/branches/*}
case " $TEST_BROKEN " in *" $repository "*) echo "gh: Bad Gateway (HTTP 502)" >&2; exit 1 ;; esac
case " $TEST_CARRYING " in
  *" $repository "*) printf '%s\n' "$(printf '%s' "$repository" | tr -c 'a-f0-9' 'a' | cut -c1-8)" ;;
  *) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
esac
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
echo "coordinated branch tests passed"
