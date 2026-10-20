#!/bin/sh
# Redocly's experimental generator is pinned and checked byte-for-byte.
set -eu
cd "$(dirname "$0")/../.."
mode=${1:---check}
case "$mode" in
    --write|--check) ;;
    *) echo "Usage: tools/clients/generate.sh [--write|--check]" >&2; exit 2 ;;
esac
cli=tools/clients/node_modules/@redocly/cli/bin/cli.js
if [ ! -f "$cli" ]; then
    echo "Install contract tools: npm --prefix tools/clients ci" >&2
    exit 1
fi
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT HUP INT TERM
node "$cli" generate-client hq/platform/api/hq-api.openapi.json \
    --output "$temporary/hq.ts" --generator typescript --generator cli \
    --server-url / --import-ext ts --args-style grouped \
    --setup tools/clients/setup.ts
if [ "$mode" = --write ]; then
    mkdir -p tools/clients/generated
    cp "$temporary/hq.ts" "$temporary/hq.cli.ts" "$temporary/hq.zod.ts" tools/clients/generated/
else
    diff -ru tools/clients/generated "$temporary"
fi
