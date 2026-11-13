#!/bin/sh
# Prove the long-lived server always starts the automation worker beside it, a
# scratch server only when asked, and each database gets a bridge socket of
# its own, so two servers of one checkout never share one.
set -eu

repo_dir="$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)"
shell="${SHELL_UNDER_TEST:-/bin/sh}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

fail() {
    echo "$1" >&2
    cat "$work/out" >&2
    exit 1
}

# The plan of a server in profile $1 on database $2; any further arguments are
# more of its environment. It exits before reading a secret or touching a file.
plan() {
    profile=$1 database=$2
    shift 2
    case "$profile" in
        secure) set -- DJANGO_DEBUG=0 DJANGO_BEHIND_TLS_PROXY=1 SEVERINO_OIDC_ENABLED=1 "$@" ;;
        scratch) set -- DJANGO_DEBUG=1 "$@" ;;
    esac
    env -i PATH="$PATH" TMPDIR="$work/tmp" HQ_DEV_PROFILE="$profile" UVICORN_HOST=127.0.0.1 UVICORN_PORT=8000 \
        SEVERINO_DATABASE_PATH="$database" usage_plan=true "$@" \
        "$shell" "$repo_dir/tools/dev/dev.sh" >"$work/out" 2>&1 || fail "the $profile plan failed"
}
bridge() { sed -n 's/^automation: the bridge at \([^,]*\),.*/\1/p' "$work/out"; }

scratch="$repo_dir/var/dev/scratch/db.sqlite3"

# 1. The long-lived server, given no flag, runs the worker.
plan secure /example/state/dev.sqlite3
long_lived=$(bridge)
[ -n "$long_lived" ] || fail "mise run dev plans no automation worker"

# 2. A scratch server runs it only with --runner.
plan scratch "$scratch"
[ -z "$(bridge)" ] || fail "a scratch server without --runner plans the automation worker"
plan scratch "$scratch" usage_runner=true
scratch_bridge=$(bridge)
[ -n "$scratch_bridge" ] || fail "a scratch server with --runner plans no automation worker"

# 3. Each database has its own socket, and a database keeps the one it had.
[ "$long_lived" != "$scratch_bridge" ] || fail "the long-lived and the scratch server share the bridge $long_lived"
case "$long_lived" in
    "$work/tmp"/*) ;;
    *) fail "the bridge $long_lived is not under TMPDIR" ;;
esac
plan secure /example/state/dev.sqlite3
[ "$(bridge)" = "$long_lived" ] || fail "a restart on one database moved its bridge from $long_lived to $(bridge)"

echo "dev server: worker beside dev always, beside scratch on --runner, one bridge per database"
