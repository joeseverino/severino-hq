#!/usr/bin/env bash
# Run Go formatting, the generated-code check, vet, and the unit tests under the race detector.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> gofmt"
out=$(gofmt -l .)
if [ -n "$out" ]; then
    echo "Files need formatting:"
    echo "$out"
    exit 1
fi

echo "==> generated types match their specs"
# Each generated file and the spec it comes from.
generated=(
    "runtime/bridge.gen.go:api/hq-controller.openapi.json"
    "providers/cfapi/cloudflare.gen.go:api/vendor/cloudflare/openapi.slice.json"
    "providers/adguardapi/adguard.gen.go:api/vendor/adguard/openapi.yaml"
    "providers/npmapi/npm.gen.go:api/vendor/npm/openapi.bundled.json"
    "providers/githubapi/github.gen.go:api/vendor/github/openapi.slice.json"
)
snapshot=$(mktemp -d)
for pair in "${generated[@]}"; do
    file=${pair%%:*}
    mkdir -p "$snapshot/$(dirname "$file")"
    cp "$file" "$snapshot/$file"
done
go generate ./...
stale=0
for pair in "${generated[@]}"; do
    file=${pair%%:*} spec=${pair#*:}
    if ! diff -u "$snapshot/$file" "$file" >&2; then
        cp "$snapshot/$file" "$file"
        echo "$file is out of date with $spec." >&2
        stale=1
    fi
done
rm -rf "$snapshot"
if [ "$stale" -ne 0 ]; then
    echo "Run: go generate ./... (in controller/) and commit the result." >&2
    exit 1
fi

echo "==> vendor slicer regressions"
"${CHECK_PYTHON:-python3}" api/vendor/test_slice.py

echo "==> go vet"
go vet ./...

echo "==> ignored advisories still do not apply"
# osv-scanner.toml ignores GO-2026-5932 because no x/crypto/openpgp package is
# imported by the controller, its tests or its generator.
if go list -deps -test ./... tool | grep -q '^golang.org/x/crypto/openpgp'; then
    echo "x/crypto/openpgp is imported; remove GO-2026-5932 from osv-scanner.toml." >&2
    exit 1
fi

echo "==> go test (race detector)"
go test -race ./...

echo "All controller checks passed cleanly."
