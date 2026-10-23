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
# Every *.gen.go in the tree, before and after go generate, so a generated file
# is checked from the moment it exists, and one generate writes anew is caught.
snapshot=$(mktemp -d)
find . -name '*.gen.go' | sort >"$snapshot/before"
while IFS= read -r file; do
    mkdir -p "$snapshot/$(dirname "$file")"
    cp "$file" "$snapshot/$file"
done <"$snapshot/before"
go generate ./...
find . -name '*.gen.go' | sort >"$snapshot/after"
stale=0
while IFS= read -r file; do
    if ! diff -u "$snapshot/$file" "$file" >&2; then
        cp "$snapshot/$file" "$file"
        echo "$file is out of date with its spec (its go:generate line names it)." >&2
        stale=1
    fi
done <"$snapshot/before"
while IFS= read -r file; do
    rm "$file"
    echo "$file is generated but not in the tree." >&2
    stale=1
done < <(comm -13 "$snapshot/before" "$snapshot/after")
if [ ! -s "$snapshot/before" ]; then
    echo "no *.gen.go found under controller/." >&2
    stale=1
fi
rm -rf "$snapshot"
if [ "$stale" -ne 0 ]; then
    echo "Run: go generate ./... (in controller/) and commit the result." >&2
    exit 1
fi

echo "==> vendor slicer regressions"
python3 api/vendor/test_slice.py

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
