#!/bin/sh
# Every derived file, from its source, in the order each one reads the last:
# the API document feeds the clients, the bridge contract feeds the Go types.
#
# Usage: mise run regen            rewrite them
#        mise run regen --check    fail on any drift, writing nothing
#
# --check leaves the Go types to `mise run controller`, which regenerates them
# against a snapshot; it needs the Go toolchain this gate does not.
set -eu
cd "$(dirname "$0")/.."
# The committed records describe the host alone.
unset SEVERINO_HQ_PLUGINS
mode="write"
case "${1:-}" in
    "") ;;
    --check) mode=check ;;
    *) echo "Usage: mise run regen [--check]" >&2; exit 2 ;;
esac

manage() {
    echo "[regen] manage.py $*"
    uv run --locked python manage.py "$@"
}

if [ "$mode" = check ]; then
    # Every drift is named, not only the first.
    stale=""
    manage sdk_contract --check || stale="$stale sdk_contract"
    manage api_openapi --check || stale="$stale api_openapi"
    echo "[regen] generate-clients --check"
    scripts/generate-clients.sh --check || stale="$stale generate-clients"
    manage bridge_contract --check || stale="$stale bridge_contract"
    manage connections_reference --check || stale="$stale connections_reference"
    if [ -n "$stale" ]; then
        echo "[regen] behind:$stale; run \`mise run regen\` and review the diff." >&2
        exit 1
    fi
    exit 0
fi

manage sdk_contract
manage api_openapi
echo "[regen] generate-clients --write"
scripts/generate-clients.sh --write
manage bridge_contract
echo "[regen] go generate ./... (controller)"
(cd controller && go generate ./...)
manage connections_reference
