#!/bin/sh
# Every derived file, from its source, in the order each one reads the last:
# the API document feeds the clients, the bridge contract feeds the Go types.
#
# Run as `mise run regen`, whose environment describes the host alone.
# `mise run regen --check` runs the drift:* gates instead; the Go types are
# held by `mise run controller`, which regenerates them against a snapshot.
set -eu
cd "$(dirname "$0")/.."

manage() {
    echo "[regen] manage.py $*"
    uv run --locked python manage.py "$@"
}

manage sdk_contract
manage api_openapi
echo "[regen] generate-clients --write"
scripts/generate-clients.sh --write
manage bridge_contract
echo "[regen] go generate ./... (controller)"
(cd controller && go generate ./...)
manage connections_reference
