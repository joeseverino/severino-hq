#!/bin/sh
# The two security gates CI runs, before pushing rather than after merging.
#
# Both fail the pipeline once `main` already has the commit, which is the
# expensive place to find out. Their settings are copied from the workflows
# rather than chosen here, so a local pass means what CI's does:
#
#   trivy   .github/workflows/ci.yml         HIGH,CRITICAL, fixable only
#   codeql  .github/workflows/codeql.yml     security-and-quality, less the
#           .github/codeql/codeql-config.yml one rule that config filters
#
# Usage:
#   ./scripts/security-scan.sh              # CodeQL only; no image needed
#   ./scripts/security-scan.sh --image REF  # also Trivy, against REF
#   ./scripts/security-scan.sh --build      # also Trivy, building the host image
set -eu
unset CDPATH

repo_root=$(cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root"

image=""
build=0
while [ $# -gt 0 ]; do
    case "$1" in
        --image) image=${2:?--image needs a reference}; shift 2 ;;
        --build) build=1; shift ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

# CodeQL ships as a `gh` extension; a `codeql` on PATH wins when there is one,
# so this works however it was installed.
if command -v codeql >/dev/null 2>&1; then
    codeql_cmd="codeql"
elif gh codeql version >/dev/null 2>&1; then
    codeql_cmd="gh codeql"
else
    echo "CodeQL is not installed. gh extension install github/gh-codeql" >&2
    exit 2
fi

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
db="$work/db"
results="$work/results.sarif"

echo "[security] CodeQL database"
# `--build-mode none` is what the Action uses for Python: nothing is compiled,
# so the extractor reads the tree directly.
$codeql_cmd database create "$db" \
    --language=python \
    --build-mode=none \
    --source-root="$repo_root" \
    --overwrite >/dev/null

echo "[security] CodeQL analysis (security-and-quality)"
$codeql_cmd database analyze "$db" \
    --format=sarif-latest \
    --output="$results" \
    --sarif-category=/language:python \
    -- codeql/python-queries:codeql-suites/python-security-and-quality.qls >/dev/null

# The rule the config filters is filtered here too, so local and CI agree on
# what counts as a finding. See .github/codeql/codeql-config.yml for why.
if ! "${CHECK_PYTHON:-python3}" scripts/codeql_report.py "$results"; then
    exit 1
fi
echo "[security] CodeQL clean"

if [ "$build" = "1" ] && [ -z "$image" ]; then
    image="severino-hq:security-scan"
    echo "[security] Building $image"
    docker build -t "$image" . >/dev/null
fi

if [ -z "$image" ]; then
    echo "[security] No image given; skipping Trivy. Pass --image REF or --build."
    exit 0
fi

if ! command -v trivy >/dev/null 2>&1; then
    echo "Trivy is not installed. brew install trivy" >&2
    exit 2
fi

echo "[security] Trivy scan of $image"
# The workflow's flags: fixable HIGH/CRITICAL only, non-zero on a hit.
trivy image \
    --severity HIGH,CRITICAL \
    --ignore-unfixed \
    --exit-code 1 \
    --format table \
    "$image"

echo "[security] all security checks passed"
