#!/usr/bin/env bash
# Run Go formatting, the generated-code check, vet, unit tests, and differential parity checks.
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

echo "==> go vet"
go vet ./...

echo "==> go test (race detector)"
go test -race ./...

echo "==> differential parity"
go test -c -o providers.test ./providers

# Discover Python interpreter
python=""
if [ -n "${CHECK_PYTHON:-}" ]; then
    python="$CHECK_PYTHON"
elif [ -n "${PYTHON:-}" ]; then
    python="$PYTHON"
elif [ -x ../.venv/bin/python ]; then
    python="../.venv/bin/python"
else
    common_dir=$(git rev-parse --git-common-dir 2>/dev/null || true)
    if [ -n "$common_dir" ]; then
        main_root=$(dirname "$common_dir")
        if [ -x "$main_root/.venv/bin/python" ]; then
            python="$main_root/.venv/bin/python"
        fi
    fi
fi

if [ -z "$python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python="$(command -v python3)"
    elif command -v python >/dev/null 2>&1; then
        python="$(command -v python)"
    else
        echo "Python is required for differential parity checks." >&2
        rm -f providers.test
        exit 1
    fi
fi

"$python" tests/parity.py ./providers.test
rm -f providers.test

echo "All controller checks passed cleanly."
