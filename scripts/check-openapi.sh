#!/bin/sh
# Validate every contract with the locked OpenAPI toolchain.
set -eu
cd "$(dirname "$0")/.."
cli=scripts/openapi/node_modules/@redocly/cli/bin/cli.js
if [ ! -f "$cli" ]; then
    echo "Install contract tools: npm --prefix scripts/openapi ci" >&2
    exit 1
fi
# Specification rules cover wire contracts. HTTP style recommendations do not
# apply to Django's canonical trailing slashes or the stdin/stdout bridge.
node "$cli" lint --extends=spec --format=summary \
    hq/platform/api/hq-api.openapi.json controller/api/hq-controller.openapi.json \
    controller/api/hq-connections.openapi.json

./scripts/generate-clients.sh --check
node --test scripts/openapi/test-client.mjs
