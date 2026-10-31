#!/usr/bin/env bash
# Go formatting, the generated-code check, the vendor slicer, golangci-lint and the advisory guard:
# the controller's static half, lint:controller. Its race tests are test:controller.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "==> gofmt"
out=$(gofmt -l .)
if [ -n "$out" ]; then
    echo "Files need formatting:"
    echo "$out"
    exit 1
fi

echo "==> go fix"
# The toolchain's own modernizers: what go fix would rewrite is code written in
# an older idiom than the pinned Go.
fixdiff=$(go fix -diff ./... 2>/dev/null || true)
if [ -n "$fixdiff" ]; then
    printf '%s\n' "$fixdiff" | sed -n 's/^+++ \(.*\) (new)$/\1/p' >&2
    echo "go fix would change the files above. Run: go fix ./... (in controller/)" >&2
    exit 1
fi

echo "==> generated types match their specs"
# Regenerate in place; a generated file that is stale, or that generate writes
# anew (deleted from the index, or never added), differs from the index.
if [ -z "$(git ls-files '*.gen.go')" ]; then
    echo "no *.gen.go found under controller/." >&2
    exit 1
fi
go generate ./...
drift=$(git ls-files --modified --others --exclude-standard -- '*.gen.go')
if [ -n "$drift" ]; then
    printf '%s\n' "$drift" >&2
    echo "Generated files differ from their specs. Run: go generate ./... (in controller/) and commit the result." >&2
    exit 1
fi

echo "==> vendor slicer regressions"
python3 api/vendor/test_slice.py

echo "==> golangci-lint"
# Its govet runs every analyzer go vet does, so vet has no step of its own;
# .golangci.yml names the linters.
golangci-lint run ./...

echo "==> ignored advisories still do not apply"
# osv-scanner.toml ignores GO-2026-5932 because no x/crypto/openpgp package is
# imported by the controller, its tests or its generator.
if go list -deps -test ./... tool | grep -q '^golang.org/x/crypto/openpgp'; then
    echo "x/crypto/openpgp is imported; remove GO-2026-5932 from osv-scanner.toml." >&2
    exit 1
fi

echo "All controller lint checks passed."
